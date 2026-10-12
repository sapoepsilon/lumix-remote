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
import logging
import os
import time
import uuid
from collections import OrderedDict
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import aiohttp
from yarl import URL

from . import didl
from .didl import BrowseError, FileItem, Res

log = logging.getLogger("lumix.files")

PAGE = 50                     # Browse page size
LIST_TTL_S = 30.0             # re-validate the cached listing after this long
DEFAULT_DEST = "~/Pictures/Lumix"
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


class ImportJob:
    def __init__(self, ids: List[str], dest: str, include_raw: bool):
        self.id = uuid.uuid4().hex[:12]
        self.ids = ids
        self.dest = dest
        self.include_raw = include_raw
        self.state = "queued"          # queued | running | done | done_with_errors | failed | cancelled
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
            "include_raw": self.include_raw, "items": len(self.ids),
            "files_total": self.files_total, "files_done": self.files_done,
            "files_skipped": self.files_skipped, "files_failed": self.files_failed,
            "bytes_total": self.bytes_total, "bytes_done": self.bytes_done,
            "progress": round(self.bytes_done / self.bytes_total, 4) if self.bytes_total else
            (1.0 if self.state == "done" else 0.0),
            "current": self.current, "errors": self.errors[-20:], "saved": self.saved[-200:],
            "created": self.created, "started": self.started, "finished": self.finished,
        }


class FileBrowser:
    def __init__(self, camera: Any, idle_timeout_s: float = 120.0, page: int = PAGE):
        self.cam = camera
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

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._idle_task is None:
            self._idle_task = asyncio.ensure_future(self._idle_loop())

    async def stop(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._idle_task
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
                "jobs": [j.id for j in self.jobs.values() if j.state in ("queued", "running")]}

    def _publish(self) -> None:
        with contextlib.suppress(Exception):
            self.cam.bus.publish("files", self.status())

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True, limit=4),
                headers={"User-Agent": "LUMIX Sync"})
        return self._session

    # ---------------------------------------------------------------- mode
    async def ensure_play(self) -> None:
        """Camera in play mode with live view paused; raises ModeError."""
        self.touch()
        if self.active and self.cam.state.get("cammode") == "play":
            return
        cam = self.cam
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

    async def leave(self, force: bool = False) -> Dict[str, Any]:
        """Back to rec mode and resume live view (unless a transfer is running)."""
        if self.pins and not force:
            return {"left": False, "reason": "%d transfer(s) running" % self.pins, **self.status()}
        cam = self.cam
        result = "not active"
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
        return {"left": True, "recmode": result, **self.status()}

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
    def start_import(self, ids: List[str], dest: Optional[str] = None,
                     include_raw: bool = True) -> ImportJob:
        if not ids:
            raise ValueError("ids is empty")
        root = os.path.abspath(os.path.expanduser(dest or DEFAULT_DEST))
        job = ImportJob([str(i) for i in ids], root, bool(include_raw))
        self.jobs[job.id] = job
        while len(self.jobs) > 50:
            k, old = next(iter(self.jobs.items()))
            if old.state in ("queued", "running"):
                break
            self.jobs.pop(k)
        job.task = asyncio.ensure_future(self._run_import(job))
        return job

    def cancel_job(self, job_id: str) -> ImportJob:
        job = self.jobs[job_id]
        if job.task and not job.task.done():
            job.task.cancel()
        return job

    async def _run_import(self, job: ImportJob) -> None:
        job.state = "running"
        job.started = time.time()
        self.cam.log_event("files", "import %s: %d item(s) -> %s" % (job.id, len(job.ids), job.dest))
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
                for it, res, name in plan:
                    folder = os.path.join(job.dest, it.date_folder())
                    path = os.path.join(folder, name)
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
            job.state = "cancelled"
        except Exception as e:
            job.state = "failed"
            job.errors.append({"error": "%s: %s" % (type(e).__name__, e)})
        finally:
            job.current = None
            job.finished = time.time()
            self.touch()
            self.cam.log_event("files", "import %s %s: %d saved, %d skipped, %d failed" % (
                job.id, job.state, job.files_done, job.files_skipped, job.files_failed))
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
