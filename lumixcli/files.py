"""`lumix files`: list, download and import the card's files through the
server's files API (the server switches the camera to play mode for browsing;
`lumix files done` switches it back to shooting).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional

from .client import CliError, Client
from .resolve import ResolveError

PAGE = 200
JOB_ACTIVE = ("queued", "running")


def _size(b: Any) -> str:
    try:
        n = float(b)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "-"


def fmt_item(it: Dict[str, Any]) -> str:
    return "%-10s %-14s %-5s %-19s %9s%s" % (
        it.get("id"), it.get("name"), it.get("kind"), (it.get("date") or "undated").replace("T", " "),
        _size(it.get("size")), "  +RAW" if it.get("has_raw") else "")


def list_page(c: Client, offset: int = 0, limit: int = 60, kind: str = "all",
              refresh: bool = False) -> Dict[str, Any]:
    q = {"offset": offset, "limit": limit, "kind": kind}
    if refresh:
        q["refresh"] = 1
    return c.get("/api/files?" + urllib.parse.urlencode(q), timeout=max(c.timeout, 60))


def iter_items(c: Client, kind: str = "all", limit: Optional[int] = None) -> Iterable[Dict[str, Any]]:
    """Newest first, paging through the card (up to ``limit`` items)."""
    offset, seen = 0, 0
    while True:
        want = PAGE if limit is None else min(PAGE, limit - seen)
        if want <= 0:
            return
        d = list_page(c, offset, want, kind)
        items = d.get("items") or []
        for it in items:
            yield it
        seen += len(items)
        offset += len(items)
        if not items or offset >= (d.get("total") or 0):
            return


def resolve_items(c: Client, refs: List[str], kind: str = "all", latest: Optional[int] = None,
                  all_: bool = False) -> List[Dict[str, Any]]:
    """IDs or card names (P1000123.JPG, case-insensitive) -> items."""
    if all_:
        return list(iter_items(c, kind))
    if latest:
        return list(iter_items(c, kind, latest))
    if not refs:
        raise ResolveError("name files by id or name, or use --latest N / --all")
    want = {r.lower(): r for r in refs}
    found: Dict[str, Dict[str, Any]] = {}
    for it in iter_items(c, kind):
        for key in (str(it.get("id")).lower(), str(it.get("name")).lower()):
            if key in want and want[key] not in found:
                found[want[key]] = it
        if len(found) == len(want):
            break
    missing = [r for r in refs if r not in found]
    if missing:
        raise ResolveError("not on the card: %s (see `lumix files list`)" % ", ".join(missing))
    return [found[r] for r in refs]


def fmt_job(j: Dict[str, Any]) -> str:
    done = (j.get("files_done") or 0) + (j.get("files_skipped") or 0)
    line = "%s %-16s %d/%d files, %s of %s" % (
        j.get("id"), j.get("state") + ("" if not j.get("paused_by") or j.get("state") != "paused"
                                       else " (%s)" % j["paused_by"]),
        done, j.get("files_total") or j.get("items") or 0, _size(j.get("bytes_done")), _size(j.get("bytes_total")))
    if j.get("files_skipped"):
        line += ", %d already there" % j["files_skipped"]
    if j.get("files_failed"):
        line += ", %d failed" % j["files_failed"]
    if j.get("folder") or j.get("dest"):
        line += "\n    -> %s" % (j.get("folder") or j.get("dest"))
    for e in (j.get("errors") or [])[-3:]:
        line += "\n    error: %s" % (e.get("error") if isinstance(e, dict) else e)
    return line


# ---------------------------------------------------------------- commands
def cmd_files(c: Client, a: argparse.Namespace) -> int:
    from .main import out
    act = a.files_cmd
    if act == "list":
        if a.all:
            items = list(iter_items(c, a.kind))
            d = {"total": len(items), "items": items}
        else:
            d = list_page(c, a.offset, a.limit, a.kind, a.refresh)
        if a.names:
            print("\n".join(it["name"] for it in d.get("items") or []))
            return 0

        def human(d: Dict[str, Any]) -> str:
            items = d.get("items") or []
            rows = ["%-10s %-14s %-5s %-19s %9s" % ("ID", "NAME", "KIND", "DATE", "SIZE")]
            rows += [fmt_item(it) for it in items]
            shown = "%d of %d" % (len(items), d.get("total") or 0)
            if not a.all and (d.get("total") or 0) > a.offset + len(items):
                shown += " (more: --offset %d, or --all)" % (a.offset + len(items))
            rows.append(shown)
            return "\n".join(rows)
        out(a, d, human)
        return 0
    if act == "status":
        d = c.get("/api/files/status")
        out(a, d, lambda d: "file browsing: %s (camera %s)%s\ndefault import folder: %s" % (
            "on" if d.get("active") else "off", d.get("cammode"),
            ", %s s until back to shooting" % d["idle_left_s"] if d.get("idle_left_s") is not None else "",
            d.get("default_dest", "?")))
        return 0
    if act == "done":
        d = c.post("/api/files/done", {})
        out(a, d, lambda d: "back to shooting" + (
            "; paused imports: %s" % ", ".join(d["paused_jobs"]) if d.get("paused_jobs") else ""))
        return 0
    if act == "download":
        items = resolve_items(c, a.ids, a.kind, a.latest, a.all)
        os.makedirs(a.output, exist_ok=True)
        saved = []
        for it in items:
            for variant in (["original", "raw"] if a.raw and it.get("raw_url") else ["original"]):
                path = "/api/files/%s/download?variant=%s" % (urllib.parse.quote(str(it["id"])), variant)
                saved.append(c.download(path, a.output, fallback=it["name"], skip_same_size=not a.force,
                                        progress=None if a.json or not sys.stderr.isatty() else sys.stderr))
        out(a, saved, lambda s: "\n".join("%s %s (%s)" % ("skipped" if x["skipped"] else "saved  ",
                                                            x["path"], _size(x["bytes"])) for x in s))
        return 0
    if act == "import":
        items = resolve_items(c, a.ids, a.kind, a.latest, a.all)
        if not items:
            raise ResolveError("nothing to import")
        body: Dict[str, Any] = {"ids": [it["id"] for it in items], "include_raw": not a.no_raw}
        if a.dest:
            body["dest"] = os.path.abspath(os.path.expanduser(a.dest))
        j = c.post("/api/files/import", body)
        if a.no_wait:
            out(a, j, fmt_job)
            return 0
        return watch_job(c, a, j["id"])
    if act == "jobs":
        d = c.get("/api/jobs")
        out(a, d, lambda d: "\n".join(fmt_job(j) for j in d) or "(no import jobs)")
        return 0
    if act in ("cancel", "pause", "resume"):
        d = c.post("/api/jobs/%s/%s" % (urllib.parse.quote(a.job), act), {})
        if act == "resume" and a.wait:
            return watch_job(c, a, a.job)
        out(a, d, fmt_job)
        return 0
    raise ResolveError("unknown files command %s" % act)


def watch_job(c: Client, a: argparse.Namespace, job_id: str) -> int:
    tty = sys.stderr.isatty() and not a.json
    last = ""
    while True:
        j = c.get("/api/jobs/%s" % urllib.parse.quote(job_id))
        st = j.get("state")
        if tty:
            done = (j.get("files_done") or 0) + (j.get("files_skipped") or 0)
            line = "\r%s %d/%d files, %s of %s %s" % (
                st, done, j.get("files_total") or 0, _size(j.get("bytes_done")), _size(j.get("bytes_total")),
                (j.get("current") or "")[:30])
            sys.stderr.write(line.ljust(len(last)))
            sys.stderr.flush()
            last = line
        if st not in JOB_ACTIVE:
            break
        time.sleep(0.5)
    if tty:
        sys.stderr.write("\n")
    if a.json:
        print(json.dumps(j, indent=2, default=str))
    else:
        print(fmt_job(j))
        if st == "paused":
            print("paused (%s); `lumix files resume %s` continues it" % (j.get("paused_by"), job_id))
    if st in ("failed", "cancelled") or j.get("files_failed"):
        raise CliError("import %s" % (st if st != "done_with_errors" else "finished with errors"), j)
    return 0


def add_parser(add: Any, common: argparse.ArgumentParser) -> None:
    sp = add("files", None, "card files: list, download, import (switches the camera to playback)")
    fs = sp.add_subparsers(dest="files_cmd", metavar="list|download|import|status|done|jobs|cancel|pause|resume")
    fs.required = True

    def sel(p: argparse.ArgumentParser, ids: bool = True) -> None:
        if ids:
            p.add_argument("ids", nargs="*", metavar="ID|NAME", help="file ids or card names (P1000123.JPG)")
            p.add_argument("--latest", "-n", type=int, metavar="N", help="the newest N files")
            p.add_argument("--all", action="store_true", help="every file on the card")
        p.add_argument("--kind", choices=["all", "photo", "video"], default="all")

    p = fs.add_parser("list", parents=[common], help="files on the card, newest first")
    sel(p, ids=False)
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--all", action="store_true", help="page through the whole card")
    p.add_argument("--refresh", action="store_true", help="re-read the card")
    p.add_argument("--names", action="store_true", help="names only, one per line")
    p.set_defaults(fn=cmd_files)
    p = fs.add_parser("download", parents=[common], help="download files to a folder (default .)")
    sel(p)
    p.add_argument("-o", "--output", default=".", metavar="DIR")
    p.add_argument("--raw", action="store_true", help="also the RAW of RAW+JPEG shots")
    p.add_argument("--force", action="store_true", help="download even if a file of that size exists")
    p.set_defaults(fn=cmd_files)
    p = fs.add_parser("import", parents=[common],
                      help="import to <dest>/<date>/ on the server's machine (default ~/Pictures/Lumix)")
    sel(p)
    p.add_argument("--dest", help="import root (default: the server's, ~/Pictures/Lumix)")
    p.add_argument("--no-raw", action="store_true", help="skip the RAW of RAW+JPEG shots")
    p.add_argument("--no-wait", action="store_true", help="start the job and return")
    p.set_defaults(fn=cmd_files)
    for name, h in (("status", "file browsing state and the default import folder"),
                    ("done", "leave file browsing: back to shooting (pauses running imports)"),
                    ("jobs", "import jobs")):
        p = fs.add_parser(name, parents=[common], help=h)
        p.set_defaults(fn=cmd_files)
    for name, h in (("cancel", "cancel an import"), ("pause", "pause an import (resumable)"),
                    ("resume", "resume a paused import")):
        p = fs.add_parser(name, parents=[common], help=h)
        p.add_argument("job")
        if name == "resume":
            p.add_argument("--wait", action="store_true", help="follow it until it finishes")
        p.set_defaults(fn=cmd_files)
