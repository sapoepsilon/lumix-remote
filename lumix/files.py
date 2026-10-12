"""Card file browsing, thumbnails, downloads and import jobs.

Mode handling
-------------
The camera only serves its ContentDirectory (and the :50001 files) in play
mode, and live view only runs in rec mode. ``FileBrowser.ensure_play()``
pauses live view (``camera.liveview_wanted = False`` keeps the watchdog from
switching back), sends ``stopstream`` + ``camcmd playmode`` under the camera
lock and waits for ``getstate`` to report ``cammode=play``. ``leave()``
(``POST /api/files/done``) or the idle timer sends ``recmode`` and hands live
view back to the watchdog. Downloads and import jobs "pin" the session so the
idle timer never switches modes underneath a transfer.

Listing
-------
Browse is recursive from ObjectID ``0`` (containers are followed), paged
(``PAGE`` items per request), and cached for the whole browsing session; a
cheap ``RequestedCount=1`` probe re-validates ``TotalMatches`` before reusing
the cache. Items are sorted newest first by ``dc:date``.

File transfer goes straight to ``http://<camera>:50001/<name>`` with its own
HTTP session, so a long download never blocks ``cam.cgi`` commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

import aiohttp
from yarl import URL

from . import didl
from .didl import BrowseError, FileItem, Res

log = logging.getLogger("lumix.files")

PAGE = 50                     # Browse page size
LIST_TTL_S = 30.0             # re-validate the cached listing after this long
# Import root; LUMIX_IMPORT_DEST overrides it (QA, other disks).
DEFAULT_DEST = os.environ.get("LUMIX_IMPORT_DEST") or "~/Pictures/Lumix"
CHUNK = 256 * 1024


class ModeError(Exception):
    """The camera refused to enter (or leave) play mode."""


class NotFound(Exception):
    pass


class _LRU:
    def __init__(self, max_bytes: int):
        self.max = max_bytes
        self.size = 0
        self.d: "OrderedDict[str, Tuple[bytes, str]]" = OrderedDict()

    def get(self, k: str) -> Optional[Tuple[bytes, str]]:
        v = self.d.get(k)
        if v is not None:
            self.d.move_to_end(k)
        return v

    def put(self, k: str, body: bytes, ctype: str) -> None:
        if k in self.d:
            self.size -= len(self.d.pop(k)[0])
        self.d[k] = (body, ctype)
        self.size += len(body)
        while self.size > self.max and self.d:
            _, (b, _) = self.d.popitem(last=False)
            self.size -= len(b)

    def clear(self) -> None:
        self.d.clear()
        self.size = 0


class _Entry:
    """One top-level card entry: a single file or a group container."""

    def __init__(self, item: Optional[FileItem] = None, cont: Optional[didl.Container] = None):
        self.item = item
        self.cont = cont
        oid = (item.object_id if item is not None else cont.object_id if cont is not None else "")
        digits = "".join(ch for ch in oid if ch.isdigit())
        self.number = int(digits) if digits else 0

    @property
    def count(self) -> int:
        return 1 if self.item is not None else (self.cont.child_count or 0) if self.cont else 0

    @property
    def kind(self) -> str:
        if self.item is not None:
            return self.item.kind
        # camera groups (time lapse "Interval", burst, stop motion) hold stills
        return "photo"


ACTIVE_JOB_STATES = ("queued", "running")


class ImportJob:
    def __init__(self, ids: List[str], dest: str, include_raw: bool, folder: Optional[str] = None,
                 label: Optional[str] = None):
        self.id = uuid.uuid4().hex[:12]
        self.ids = ids
        self.dest = dest
        self.include_raw = include_raw
        self.folder = folder           # save every file here (flat) instead of <dest>/<date>/
        self.label = label             # e.g. "timelapse sunset" (UI / power-off sheet)
        # queued | running | paused | done | done_with_errors | failed | cancelled
        self.state = "queued"
        self.paused_by: Optional[str] = None   # "user" | "shooting" | "poweroff" | "server-stop" ...
        self.paused_at: Optional[float] = None
        self._pause_req: Optional[str] = None
        self.created = time.time()
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.files_total = 0
        self.files_done = 0            # downloaded
        self.files_skipped = 0         # already present (same name + size)
        self.files_failed = 0
        self.bytes_total = 0
        self.bytes_done = 0
        self.current: Optional[str] = None
        self.errors: List[Dict[str, str]] = []
        self.saved: List[str] = []
        self.task: Optional[asyncio.Task] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "type": "import", "state": self.state, "dest": self.dest,
            "folder": self.folder, "label": self.label,
            "paused_by": self.paused_by, "paused_at": self.paused_at,
            "include_raw": self.include_raw, "items": len(self.ids),
            "files_total": self.files_total, "files_done": self.files_done,
            "files_skipped": self.files_skipped, "files_failed": self.files_failed,
            "bytes_total": self.bytes_total, "bytes_done": self.bytes_done,
            "progress": round(self.bytes_done / self.bytes_total, 4) if self.bytes_total else
            (1.0 if self.state == "done" else 0.0),
            "current": self.current, "errors": self.errors[-20:], "saved": self.saved[-200:],
            "created": self.created, "started": self.started, "finished": self.finished,
        }

    def checkpoint(self) -> Dict[str, Any]:
        """Everything needed to resume the job after a server restart."""
        d = self.to_dict()
        d.update(ids=self.ids, saved=self.saved, errors=self.errors[-50:])
        return d

    @classmethod
    def from_checkpoint(cls, d: Dict[str, Any]) -> "ImportJob":
        j = cls([str(i) for i in d.get("ids") or []], d["dest"], bool(d.get("include_raw", True)),
                d.get("folder"), d.get("label"))
        j.id = d.get("id") or j.id
        for k in ("state", "paused_by", "paused_at", "created", "started", "finished",
                  "files_total", "files_done", "files_skipped", "files_failed"):
            if k in d:
                setattr(j, k, d[k])
        j.saved = list(d.get("saved") or [])
        j.errors = list(d.get("errors") or [])
        if j.state in ACTIVE_JOB_STATES:
            # the server stopped (or crashed) in the middle of it: resumable
            j.state, j.paused_by = "paused", j.paused_by or "server-stop"
            j.paused_at = j.paused_at or time.time()
        return j


class FileBrowser:
    def __init__(self, camera: Any, idle_timeout_s: float = 120.0, page: int = PAGE,
                 jobs_path: Optional[str] = None):
        self.cam = camera
        self.jobs_path = jobs_path     # paused/running import jobs survive a server restart
        self.idle_timeout_s = idle_timeout_s
        self.page = page
        self.active = False            # we put the camera in play mode
        self.last_used = 0.0
        self.pins = 0                  # running downloads / jobs
        self.entries: Optional[List[_Entry]] = None   # card root, newest first
        self.children: Dict[str, Dict[int, FileItem]] = {}   # group id -> index -> item
        self.by_id: Dict[str, FileItem] = {}
        self.total_probe: Optional[int] = None
        self.listed_at = 0.0
        self._list_lock = asyncio.Lock()
        self._fetch_sem = asyncio.Semaphore(2)        # concurrent :50001 requests
        self._session: Optional[aiohttp.ClientSession] = None
        self._idle_task: Optional[asyncio.Task] = None
        self.thumbs = _LRU(48 << 20)
        self.previews = _LRU(64 << 20)
        self.jobs: "OrderedDict[str, ImportJob]" = OrderedDict()
        self.last_error: Optional[str] = None
        # Quiesce hooks (server.quiesce): entering play for the user pauses the
        # timelapse and stops a recording; leaving it resumes the timelapse.
        self.on_enter: Optional[Callable[[], Awaitable[Any]]] = None
        self.on_leave: Optional[Callable[[], Awaitable[Any]]] = None
        self._quiet = 0                # >0: internal play-mode use (timelapse import), no hooks
        self._load_jobs()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._idle_task is None:
            self._idle_task = asyncio.ensure_future(self._idle_loop())

    async def stop(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._idle_task
        # running imports are checkpointed (resumable), not lost
        await self.pause_jobs("server-stop", timeout=3.0)
        for j in self.jobs.values():
            if j.task and not j.task.done():
                j.task.cancel()
        if self.active:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.leave(force=True), 8)
        if self._session is not None:
            await self._session.close()

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                if (self.active and self.pins == 0
                        and time.time() - self.last_used > self.idle_timeout_s):
                    log.info("files: idle for %.0fs, back to rec mode", self.idle_timeout_s)
                    await self.leave()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never die
                log.warning("files idle loop: %s", e)

    def touch(self) -> None:
        self.last_used = time.time()

    def status(self) -> Dict[str, Any]:
        return {"active": self.active, "cammode": self.cam.state.get("cammode"),
                "pins": self.pins, "idle_timeout_s": self.idle_timeout_s,
                "idle_left_s": (max(0.0, round(self.idle_timeout_s - (time.time() - self.last_used), 1))
                                if self.active else None),
                "cached_items": len(self.by_id) if self.entries is not None else None,
                "card_files": sum(e.count for e in self.entries) if self.entries is not None else None,
                "listed_at": self.listed_at or None, "last_error": self.last_error,
                "jobs": [j.id for j in self.jobs.values() if j.state in ("queued", "running")],
                "default_dest": os.path.abspath(os.path.expanduser(DEFAULT_DEST))}

    def _publish(self) -> None:
        with contextlib.suppress(Exception):
            self.cam.bus.publish("files", self.status())

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True, limit=4),
                headers={"User-Agent": "LUMIX Sync"})
        return self._session

    @contextlib.asynccontextmanager
    async def quiet(self) -> AsyncIterator[None]:
        """Internal play-mode use (timelapse frame import): no quiesce hooks."""
        self._quiet += 1
        try:
            yield
        finally:
            self._quiet -= 1

    # ---------------------------------------------------------------- mode
    async def ensure_play(self) -> None:
        """Camera in play mode with live view paused; raises ModeError."""
        self.touch()
        if self.active and self.cam.state.get("cammode") == "play":
            return
        cam = self.cam
        if getattr(cam, "interval_active", False):
            raise ModeError("an in-camera interval run is in progress; stop the timelapse first")
        if not self.active and not self._quiet and self.on_enter is not None:
            # files <- shooting: pause the timelapse, stop a recording (server.quiesce)
            await self.on_enter()
        # camera reconnecting (e.g. a power cycle): wait a little, then fail fast
        for _ in range(40):
            if getattr(cam, "status", "ready") == "ready":
                break
            await asyncio.sleep(0.5)
        else:
            raise ModeError("camera not connected (%s)" % cam.status)
        if getattr(cam, "wedged", False) and cam.state.get("cammode") != "play":
            raise ModeError("camera is stuck answering err_busy to mode switches; "
                            "power-cycle the camera")
        if getattr(cam, "body_busy", False):
            raise ModeError("camera is being operated on the body; try again when it is done")
        async with cam.exclusive("files") as t:
            if not self.active:
                self.entries = None   # new browsing session: the card may have changed
                self.children = {}    # (by_id is kept: object ids are stable for a card)
            cam.liveview_wanted = False
            self.active = True
            await cam.stop_liveview("file browsing")
            if cam.state.get("cammode") == "play":
                ok, last = True, "already in play"
            else:
                ok, last = await self._switch(t, "playmode", "play")
            if not ok:
                cam.liveview_wanted = True
                self.active = False
                self.last_error = "playmode -> %s" % last
                self._publish()
                raise ModeError("camera refused play mode (%s)" % last)
            self.last_error = None
            cam.log_event("files", "play mode for file browsing (live view paused)")
            ci = await t.cgi("get_content_info")   # as LUMIX Sync does before browsing
            log.info("get_content_info -> %s %s", ci.result, ci.text[-160:].replace("\n", " "))
            await asyncio.sleep(0.5)  # CDS answers 701 for a moment after the switch
        self._publish()
        if not self._quiet:
            # imports checkpointed when the user went back to shooting continue now
            for jid in self.resume_jobs("shooting"):
                cam.log_event("files", "import %s resumed (file browsing again)" % jid)

    async def _switch(self, t: Any, cmd: str, want: str) -> Tuple[bool, str]:
        """camcmd playmode/recmode with LUMIX Sync's retry rule (5 x 1 s on
        err_busy), then wait for getstate to agree."""
        cam = self.cam
        r = await cam.cmd("camcmd", value=cmd, timeout=10)
        last = r.result or r.text[:40]
        for _ in range(10):
            await asyncio.sleep(0.4)
            with contextlib.suppress(Exception):
                await cam._refresh_state()
            if cam.state.get("cammode") == want:
                return True, last
        return False, last

    async def leave(self, force: bool = False, pause_jobs: bool = False) -> Dict[str, Any]:
        """Back to rec mode and resume live view (unless a transfer is running).
        ``pause_jobs``: checkpoint running imports first (resumable later)."""
        paused: List[str] = []
        if pause_jobs:
            paused = await self.pause_jobs("shooting")
        if self.pins and not force:
            return {"left": False, "reason": "%d transfer(s) running" % self.pins, **self.status()}
        cam = self.cam
        result = "not active"
        was_active = self.active
        if self.active:
            async with cam.exclusive("files"):
                # leaving playback = LUMIX Sync's live-view start (stopstream, recmode, startstream)
                self.active = False
                cam.liveview_wanted = True
                ok = await cam.start_liveview("files done")
                result = "ok" if ok else "failed (cammode=%s)" % cam.state.get("cammode")
                cam.log_event("files", "left file browsing: recmode -> %s, cammode=%s"
                              % (result, cam.state.get("cammode")))
        self._publish()
        if was_active and not self._quiet and self.on_leave is not None:
            with contextlib.suppress(Exception):
                await self.on_leave()
        return {"left": True, "recmode": result, "paused_jobs": paused, **self.status()}

    @contextlib.asynccontextmanager
    async def pinned(self) -> AsyncIterator[None]:
        self.pins += 1
        self.touch()
        try:
            yield
        finally:
            self.pins -= 1
            self.touch()

    # ------------------------------------------------------------- listing
    async def _browse(self, object_id: str, start: int, count: int) -> didl.BrowseResult:
        sess = await self._sess()
        url = "http://%s:%d%s" % (self.cam.ip, _port(self.cam, "upnp_port", 60606), didl.CDS_PATH)
        headers = {"SOAPACTION": didl.SOAP_ACTION, "Content-Type": 'text/xml; charset="utf-8"',
                   "User-Agent": didl.UA}
        body = didl.browse_body(object_id, start, count)
        for attempt in range(3):
            try:
                async with sess.post(url, data=body, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=20)) as r:
                    raw = await r.read()
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                if attempt == 2:
                    raise ModeError("Browse failed: %s: %s" % (type(e).__name__, e))
                await asyncio.sleep(1)
                continue
            try:
                return didl.parse_browse_response(raw)
            except BrowseError as e:
                # 701 right after playmode: the server is not ready yet
                if e.code == "701" and attempt < 2:
                    await asyncio.sleep(1.5)
                    continue
                raise
        raise ModeError("Browse failed")

    async def _root(self) -> List["_Entry"]:
        """Top level of the card: single items and group containers, newest first."""
        entries: List[_Entry] = []
        start = 0
        total = 0
        while True:
            res = await self._browse("0", start, self.page)
            items, conts = didl.parse_didl(res.didl)
            for it in items:
                entries.append(_Entry(item=it))
            for c in conts:
                entries.append(_Entry(cont=c))
            start += res.returned
            total = res.total
            if res.returned == 0 or start >= res.total:
                break
        self.total_probe = total
        # The camera lists in file-number order (= shooting order); newest first.
        entries.sort(key=lambda e: e.number, reverse=True)
        return entries

    async def _ensure_root(self, refresh: bool) -> List["_Entry"]:
        async with self._list_lock:
            if self.entries is not None and not refresh and time.time() - self.listed_at > LIST_TTL_S:
                probe = await self._browse("0", 0, 1)
                if probe.total != self.total_probe:
                    refresh = True
                else:
                    self.listed_at = time.time()
            if self.entries is None or refresh:
                t0 = time.time()
                self.entries = await self._root()
                self.children = {}
                self.listed_at = time.time()
                for e in self.entries:
                    if e.item is not None:
                        self.by_id[e.item.id] = e.item
                log.info("files: card root has %d entries (%d files) in %.1fs", len(self.entries),
                         sum(e.count for e in self.entries), time.time() - t0)
            return self.entries

    async def _children(self, c: didl.Container, lo: int, hi: int) -> Dict[int, FileItem]:
        """Child items with container index lo..hi-1 (cached per container)."""
        cache = self.children.setdefault(c.object_id, {})
        missing = [i for i in range(lo, hi) if i not in cache]
        while missing:
            a = missing[0]
            n = min(self.page, hi - a)
            res = await self._browse(c.object_id, a, n)
            items, _ = didl.parse_didl(res.didl)
            for k, it in enumerate(items):
                cache[a + k] = it
                self.by_id[it.id] = it
            if not items:
                break
            missing = [i for i in missing if i not in cache]
        return cache

    async def list(self, offset: int = 0, limit: int = 60, kind: str = "all",
                   refresh: bool = False) -> Dict[str, Any]:
        """Newest first. Groups (time lapse etc.) are expanded lazily, so a page
        costs one Browse per touched group, never a walk of the whole card."""
        if kind not in ("all", "photo", "video"):
            raise ValueError("kind must be all|photo|video")
        await self.ensure_play()
        entries = await self._ensure_root(refresh)
        sel = entries if kind == "all" else [e for e in entries if e.kind == kind]
        total = sum(e.count for e in sel)
        offset, limit = max(0, int(offset)), max(1, min(500, int(limit)))
        out: List[FileItem] = []
        pos = 0
        for e in sel:
            if len(out) >= limit:
                break
            if pos + e.count <= offset:
                pos += e.count
                continue
            if e.item is not None:
                out.append(e.item)
                pos += 1
                continue
            c = e.cont
            assert c is not None
            # newest first inside the group = highest child index first
            first = max(0, offset - pos)                  # skip within this group
            take = min(e.count - first, limit - len(out))
            hi = e.count - first
            lo = hi - take
            kids = await self._children(c, lo, hi)
            out.extend(kids[i] for i in range(hi - 1, lo - 1, -1) if i in kids)
            pos += e.count
            self.touch()
        self.touch()
        return {"total": total, "items": [self.item_dict(i) for i in out]}

    @staticmethod
    def item_dict(i: FileItem) -> Dict[str, Any]:
        d = i.to_dict()
        base = "/api/files/%s" % i.id
        d.update(thumb_url=base + "/thumb", preview_url=base + "/preview",
                 download_url=base + "/download")
        if i.raw is not None:
            d["raw_url"] = base + "/download?variant=raw"
        return d

    async def get_item(self, item_id: str) -> FileItem:
        it = self.by_id.get(item_id)
        if it is not None:
            return it
        await self.ensure_play()
        entries = await self._ensure_root(False)
        it = self.by_id.get(item_id)
        if it is None and item_id.isdigit():
            # A group's children are numbered upwards from the group's own number
            # (crossing folder roll-overs, e.g. 109-0992 .. 111-2071), so the
            # owner is the newest group whose number is <= the id.
            n = int(item_id)
            for e in entries:   # newest first
                if e.cont is not None and e.number <= n:
                    await self._children(e.cont, 0, e.count)
                    it = self.by_id.get(item_id)
                    break
        if it is None:
            raise NotFound(item_id)
        return it

    # ------------------------------------------------------------ transfer
    def _url(self, res: Res) -> URL:
        # The camera puts its own IP in <res>; trust only the path.
        u = URL(res.url)
        fp = _port(self.cam, "files_port", 50001)
        return URL.build(scheme="http", host=self.cam.ip, port=fp if fp != 50001 else (u.port or 50001),
                         path=u.path, encoded=True)

    async def image(self, item_id: str, which: str) -> Tuple[bytes, str]:
        """Thumbnail or preview JPEG (cached)."""
        cache = self.thumbs if which == "thumb" else self.previews
        hit = cache.get(item_id)
        if hit is not None:
            self.touch()
            return hit
        it = await self.get_item(item_id)
        res = it.thumb if which == "thumb" else it.preview
        if res is None:
            raise NotFound("%s has no %s" % (item_id, which))
        await self.ensure_play()
        sess = await self._sess()
        async with self.pinned(), self._fetch_sem:
            last: Exception = NotFound(item_id)
            for attempt in range(3):
                try:
                    async with sess.get(self._url(res), timeout=aiohttp.ClientTimeout(total=30)) as r:
                        if r.status != 200:
                            raise ModeError("camera HTTP %d for %s" % (r.status, res.name))
                        body = await r.read()
                        ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
                    cache.put(item_id, body, ctype)
                    return body, ctype
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ModeError) as e:
                    last = e
                    await asyncio.sleep(0.5 * (attempt + 1))
            raise ModeError("fetching %s failed: %s" % (which, last))

    def pick(self, it: FileItem, variant: str) -> Tuple[Res, str]:
        if variant == "raw":
            if it.raw is None:
                raise NotFound("%s has no RAW" % it.id)
            return it.raw, it.raw_name or it.raw.name
        if variant != "original":
            raise ValueError("variant must be original|raw")
        if it.original is None:
            raise NotFound("%s has no original" % it.id)
        return it.original, it.name

    @contextlib.asynccontextmanager
    async def open_stream(self, item_id: str, variant: str = "original",
                          range_header: Optional[str] = None
                          ) -> AsyncIterator[Tuple[aiohttp.ClientResponse, str, Res]]:
        """Open the camera's HTTP response for a file (Range passed through)."""
        it = await self.get_item(item_id)
        res, name = self.pick(it, variant)
        await self.ensure_play()
        sess = await self._sess()
        headers = {"Range": range_header} if range_header else {}
        async with self.pinned(), self._fetch_sem:
            async with sess.get(self._url(res), headers=headers,
                                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10,
                                                              sock_read=60)) as r:
                yield r, name, res

    # ------------------------------------------------------------- import
    def new_job(self, ids: List[str], dest: Optional[str] = None, include_raw: bool = True,
                folder: Optional[str] = None, label: Optional[str] = None) -> ImportJob:
        if not ids:
            raise ValueError("ids is empty")
        root = os.path.abspath(os.path.expanduser(dest or DEFAULT_DEST))
        return ImportJob([str(i) for i in ids], root, bool(include_raw),
                         os.path.abspath(os.path.expanduser(folder)) if folder else None, label)

    def start_import(self, ids: List[str], dest: Optional[str] = None,
                     include_raw: bool = True) -> ImportJob:
        job = self.new_job(ids, dest, include_raw)
        self._add_job(job)
        job.task = asyncio.ensure_future(self._run_import(job))
        return job

    def _add_job(self, job: ImportJob) -> None:
        self.jobs[job.id] = job
        while len(self.jobs) > 50:
            k, old = next(iter(self.jobs.items()))
            if old.state in ACTIVE_JOB_STATES + ("paused",):
                break
            self.jobs.pop(k)
        self._save_jobs()

    def cancel_job(self, job_id: str) -> ImportJob:
        job = self.jobs[job_id]
        if job.task and not job.task.done():
            job.task.cancel()
        elif job.state == "paused":
            job.state, job.finished = "cancelled", time.time()
            self._save_jobs()
        return job

    # ------------------------------------------------------- pause / resume
    def running_jobs(self) -> List[ImportJob]:
        return [j for j in self.jobs.values() if j.state in ACTIVE_JOB_STATES]

    def paused_jobs(self, by: Optional[str] = None) -> List[ImportJob]:
        return [j for j in self.jobs.values()
                if j.state == "paused" and (by is None or j.paused_by == by)]

    async def pause_job(self, job_id: str, by: str = "user", timeout: float = 5.0) -> ImportJob:
        """Checkpoint a running import: the transfer stops, the ``.part`` file
        stays and is resumed with Range later. Already saved files are kept."""
        job = self.jobs[job_id]
        if job.state not in ACTIVE_JOB_STATES:
            return job
        job._pause_req = by
        t = job.task
        if t is not None and not t.done():
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await asyncio.wait_for(asyncio.shield(t), timeout)
        if job.state in ACTIVE_JOB_STATES:      # never started
            job.state, job.paused_by, job.paused_at = "paused", by, time.time()
            self._save_jobs()
        return job

    async def pause_jobs(self, by: str, timeout: float = 5.0) -> List[str]:
        ids = [j.id for j in self.running_jobs()]
        for i in ids:
            await self.pause_job(i, by, timeout)
        return ids

    def resume_job(self, job_id: str) -> ImportJob:
        job = self.jobs[job_id]
        if job.state != "paused":
            raise ValueError("job %s is %s, not paused" % (job_id, job.state))
        job.state, job.paused_by, job.paused_at, job.finished = "queued", None, None, None
        job.task = asyncio.ensure_future(self._run_import(job))
        self._save_jobs()
        return job

    def resume_jobs(self, by: Optional[str] = None) -> List[str]:
        out = []
        for j in self.paused_jobs(by):
            self.resume_job(j.id)
            out.append(j.id)
        return out

    def _load_jobs(self) -> None:
        if not self.jobs_path or not os.path.exists(self.jobs_path):
            return
        try:
            with open(self.jobs_path) as f:
                data = json.load(f)
            for d in data.get("jobs") or []:
                j = ImportJob.from_checkpoint(d)
                self.jobs[j.id] = j
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("cannot load import jobs from %s: %s", self.jobs_path, e)

    def _save_jobs(self) -> None:
        """Persist jobs that can still be resumed (queued/running/paused)."""
        if not self.jobs_path:
            return
        keep = [j.checkpoint() for j in self.jobs.values()
                if j.state in ACTIVE_JOB_STATES + ("paused",)]
        try:
            os.makedirs(os.path.dirname(self.jobs_path), exist_ok=True)
            tmp = self.jobs_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"version": 1, "jobs": keep}, f, indent=1, default=str)
            os.replace(tmp, self.jobs_path)
        except OSError as e:
            log.warning("cannot save import jobs: %s", e)

    async def run_job(self, job: ImportJob) -> ImportJob:
        """Run an import inline (the caller's task), e.g. timelapse frames."""
        await self._run_import(job, publish=False)
        return job

    async def _run_import(self, job: ImportJob, publish: bool = True) -> None:
        resumed = bool(job.saved) or job.started is not None
        job.state = "running"
        job.started = job.started or time.time()
        job.current = None
        done_paths = set(job.saved)
        if resumed:          # recounted below; saved files stay counted in files_done
            job.files_skipped = job.files_failed = 0
        if publish:
            self._save_jobs()
        self.cam.log_event("files", "import %s: %d item(s) -> %s%s" % (
            job.id, len(job.ids), job.folder or job.dest, " (resumed)" if resumed else ""))
        try:
            async with self.pinned():
                await self.ensure_play()
                plan: List[Tuple[FileItem, Res, str]] = []
                for iid in job.ids:
                    try:
                        it = await self.get_item(iid)
                    except NotFound:
                        job.errors.append({"id": iid, "error": "unknown id"})
                        job.files_failed += 1
                        continue
                    res, name = self.pick(it, "original")
                    plan.append((it, res, name))
                    if job.include_raw and it.raw is not None:
                        plan.append((it, it.raw, it.raw_name or it.raw.name))
                job.files_total = len(plan)
                job.bytes_total = sum(r.size or 0 for _, r, _ in plan)
                job.bytes_done = 0
                for it, res, name in plan:
                    folder = job.folder or os.path.join(job.dest, it.date_folder())
                    path = os.path.join(folder, name)
                    if path in done_paths:          # saved before a pause
                        job.bytes_done += res.size or 0
                        continue
                    job.current = name
                    try:
                        if await self._download_to(job, res, path):
                            job.files_done += 1
                            job.saved.append(path)
                        else:
                            job.files_skipped += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning("import %s: %s failed: %s", job.id, name, e)
                        job.files_failed += 1
                        job.errors.append({"id": it.id, "name": name, "error": "%s: %s" % (type(e).__name__, e)})
                job.state = "done" if job.files_failed == 0 else "done_with_errors"
        except asyncio.CancelledError:
            if job._pause_req:
                job.state, job.paused_by, job.paused_at = "paused", job._pause_req, time.time()
            else:
                job.state = "cancelled"
            if not publish:
                raise
        except Exception as e:
            job.state = "failed"
            job.errors.append({"error": "%s: %s" % (type(e).__name__, e)})
        finally:
            job._pause_req = None
            job.current = None
            job.finished = time.time() if job.state != "paused" else None
            self.touch()
            self.cam.log_event("files", "import %s %s%s: %d saved, %d skipped, %d failed" % (
                job.id, job.state, " (%s)" % job.paused_by if job.state == "paused" else "",
                job.files_done, job.files_skipped, job.files_failed))
            if publish:
                self._save_jobs()
                self.cam.bus.publish("job", job.to_dict())

    async def _download_to(self, job: ImportJob, res: Res, path: str) -> bool:
        """Download res to path. False = skipped (same name and size already there)."""
        size = res.size
        if os.path.exists(path) and size is not None and os.path.getsize(path) == size:
            job.bytes_done += size
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        part = path + ".part"
        sess = await self._sess()
        for attempt in range(4):
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if size is not None and have > size:
                os.remove(part)
                have = 0
            headers = {"Range": "bytes=%d-" % have} if have else {}
            counted = have
            job.bytes_done += have
            try:
                await self.ensure_play()
                async with self._fetch_sem:
                    async with sess.get(self._url(res), headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10,
                                                                      sock_read=60)) as r:
                        if r.status == 200 and have:
                            have = 0          # camera ignored Range: start over
                            job.bytes_done -= counted
                            counted = 0
                        elif r.status not in (200, 206):
                            raise ModeError("camera HTTP %d for %s" % (r.status, res.name))
                        if size is None and r.content_length is not None:
                            size = r.content_length + have
                            job.bytes_total += size
                        with open(part, "ab" if have else "wb") as f:
                            async for chunk in r.content.iter_chunked(CHUNK):
                                f.write(chunk)
                                job.bytes_done += len(chunk)
                                counted += len(chunk)
                                self.touch()
                if size is None or os.path.getsize(part) == size:
                    os.replace(part, path)
                    return True
                raise ModeError("short read %d/%s" % (os.path.getsize(part), size))
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ModeError) as e:
                job.bytes_done -= counted   # re-counted from the .part size on retry
                if attempt == 3:
                    raise
                log.info("download %s retry %d: %s", res.name, attempt + 1, e)
                await asyncio.sleep(1.5 * (attempt + 1))
        return False


def _port(cam: Any, name: str, default: int) -> int:
    """Camera-side port from the camera config (emulators override them)."""
    return int(getattr(getattr(cam, "cfg", None), name, None) or default)
