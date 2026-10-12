"""Quiesce: bring a camera session to a safe, resumable stop before something
disruptive happens, and resume what was paused afterwards.

Used by
  * camera power-off (``POST /api/power/off``, ``lumix poweroff --yes``),
  * server stop (SIGTERM / SIGINT -> aiohttp on_shutdown),
  * camera removal (``DELETE /api/cameras/{id}``),
  * switching files <-> shooting (entering file browsing pauses the timelapse
    and stops a recording; leaving it checkpoints running imports).

Order (each step bounded by a timeout, all of it by an overall deadline):
  1. import jobs: checkpointed (``.part`` files stay; resumed with Range later)
  2. timelapse: paused after the shot in flight (in-camera runs: camctrl pause)
  3. video recording: ``video_recstop``, then wait until the card write is done
  4. live view: stopstream (only a stream we started)
  5. UNSUBSCRIBE the event subscription
Steps 4-5 only for power-off / server stop / removal; then the caller acts.

Resume: when the camera reaches "ready" again (``Camera.ready_hooks``) and the
camera's ``settings.resume_on_reconnect`` is on (default), jobs paused by
power-off / server stop resume: the timelapse first; imports only when no
timelapse is running (else they wait for the next file browsing session).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Dict, List

log = logging.getLogger("server.quiesce")

HARD = ("poweroff", "server-stop", "remove")
REASONS = HARD + ("files", "shooting")
LABELS = {"poweroff": "camera power-off", "server-stop": "server stop", "remove": "camera removal",
          "files": "file browsing", "shooting": "back to shooting"}


def _import_label(j: Any) -> str:
    what = j.label or "Import"
    if j.files_total:
        return "%s: %d of %d files" % (what, j.files_done + j.files_skipped, j.files_total)
    return "%s: %d item(s)" % (what, len(j.ids))


def plan(sess: Any, reason: str = "poweroff") -> Dict[str, Any]:
    """What a quiesce for ``reason`` would pause or stop right now."""
    if reason not in REASONS:
        raise ValueError("reason must be one of %s" % ", ".join(REASONS))
    cam, fb, tl = sess.cam, sess.files, sess.timelapse
    items: List[Dict[str, Any]] = []
    if reason != "files":
        for j in fb.running_jobs():
            items.append({"kind": "import", "id": j.id, "action": "pause", "resumable": True,
                          "label": _import_label(j),
                          "detail": "Pauses after the current chunk; the partial file resumes later."})
    if reason != "shooting" and cam.state.get("rec") == "on":
        items.append({"kind": "recording", "action": "stop", "resumable": False,
                      "label": "Video recording",
                      "detail": "Stopped; waits until the camera has finished writing the card."})
    j = tl.job
    if j is not None and j.state in ("scheduled", "running") and reason != "shooting":
        tgt = j.target
        items.append({"kind": "timelapse", "id": j.id, "action": "pause", "resumable": True,
                      "label": "Timelapse %s: %d%s shots" % (j.name, j.shots, " of %d" % tgt if tgt else ""),
                      "detail": "Paused after the current shot; continues when the camera is back."
                      if reason in HARD else "Paused while you browse files; continues afterwards."})
    if reason in HARD:
        if cam._lv_running or cam.liveview.age is not None and cam.liveview.age < 5:
            items.append({"kind": "liveview", "action": "stop", "resumable": True, "label": "Live view"})
        if cam.listener.sid:
            items.append({"kind": "events", "action": "unsubscribe", "resumable": True,
                          "label": "Camera event subscription"})
    return {"reason": reason, "label": LABELS[reason], "items": items,
            "resume_on_reconnect": sess.resume_on_reconnect()}


async def quiesce(sess: Any, reason: str, timeout: float = 15.0) -> Dict[str, Any]:
    """Pause/stop everything for ``reason``; returns a report of what happened."""
    if reason not in REASONS:
        raise ValueError("reason must be one of %s" % ", ".join(REASONS))
    cam, fb, tl = sess.cam, sess.files, sess.timelapse
    t0 = time.monotonic()
    deadline = t0 + timeout
    rep: Dict[str, Any] = {"reason": reason, "paused": [], "stopped": [], "errors": []}

    def left(cap: float) -> float:
        return max(0.5, min(cap, deadline - time.monotonic()))

    async def step(name: str, coro: Any, cap: float) -> Any:
        try:
            return await asyncio.wait_for(coro, left(cap))
        except asyncio.TimeoutError:
            rep["errors"].append("%s: timed out" % name)
        except Exception as e:  # report, never abort the quiesce
            rep["errors"].append("%s: %s: %s" % (name, type(e).__name__, e))
        return None

    # 1. imports (checkpoint)
    if reason != "files":
        running = list(fb.running_jobs())
        ids = await step("imports", fb.pause_jobs(reason, timeout=left(5)), 6) or []
        for j in running:
            if j.id in ids:
                rep["paused"].append({"kind": "import", "id": j.id, "label": _import_label(j)})
    # 2. timelapse
    j = tl.job
    if j is not None and j.state in ("scheduled", "running") and reason != "shooting":
        await step("timelapse", tl.pause(reason, timeout=left(10)), 12)
        if tl.job is not None and tl.job.state == "paused":
            rep["paused"].append({"kind": "timelapse", "id": j.id,
                                  "label": "Timelapse %s (%d shots)" % (j.name, j.shots)})
    # 3. recording
    if reason != "shooting" and cam.state.get("rec") == "on":
        r = await step("recording", cam.stop_recording(timeout=left(20)), 22)
        if r and r.get("stopped"):
            rep["stopped"].append({"kind": "recording", "label": "Video recording",
                                   "card_written": r.get("card_written")})
        elif r is not None:
            rep["errors"].append("recording: video_recstop -> %s" % r.get("result"))
    if reason in HARD:
        # file browsing ends with the session; after a reconnect live view comes back
        if fb.active:
            fb.active = False
            cam.liveview_wanted = True
        # 4. live view
        if cam._lv_running:
            await step("liveview", cam.stop_liveview(LABELS[reason]), 5)
            rep["stopped"].append({"kind": "liveview", "label": "Live view"})
        # 5. UNSUBSCRIBE
        had = await step("unsubscribe", cam.unsubscribe_events(), 4)
        if had:
            rep["stopped"].append({"kind": "events", "label": "Camera event subscription"})
    rep["elapsed_s"] = round(time.monotonic() - t0, 2)
    if rep["paused"] or rep["stopped"]:
        cam.log_event("quiesce", "%s: paused %s; stopped %s%s" % (
            LABELS[reason], ", ".join(x["label"] for x in rep["paused"]) or "nothing",
            ", ".join(x["label"] for x in rep["stopped"]) or "nothing",
            "; errors: " + "; ".join(rep["errors"]) if rep["errors"] else ""))
    cam.bus.publish("quiesce", rep)
    return rep


async def resume_after_reconnect(sess: Any, force: bool = False) -> Dict[str, Any]:
    """Camera is ready again: resume what power-off / server stop paused.
    ``force`` (POST /api/resume) ignores the resume_on_reconnect setting."""
    out: Dict[str, Any] = {"timelapse": False, "imports": []}
    if not force and not sess.resume_on_reconnect():
        return out
    tl, fb = sess.timelapse, sess.files
    if not force:
        await asyncio.sleep(0.5)    # let the connect's live-view start settle
    with contextlib.suppress(Exception):
        out["timelapse"] = await tl.auto_resume("reconnect")
    cam = sess.cam
    waiting = [j for j in fb.paused_jobs() if j.paused_by in ("poweroff", "server-stop", "reconnect")
               and (force or (cam.status == "ready" and (cam.connected_at or 0) >= (j.paused_at or 0)))]
    if waiting:
        if tl.is_running():
            # the timelapse needs rec mode: imports continue in the next file browsing session
            for j in waiting:
                j.paused_by = "shooting"
            fb._save_jobs()
        else:
            for j in waiting:
                with contextlib.suppress(Exception):
                    fb.resume_job(j.id)
                    out["imports"].append(j.id)
    if out["timelapse"] or out["imports"]:
        sess.cam.log_event("quiesce", "camera back: resumed %s" % ", ".join(
            (["timelapse"] if out["timelapse"] else []) + ["import %s" % i for i in out["imports"]]))
    return out


async def power_off(sess: Any) -> Dict[str, Any]:
    """Quiesce, then ``camcmd poweroff``. If the camera refuses, reconnect (the
    event subscription is gone) and let the resume hook continue paused work."""
    rep = await quiesce(sess, "poweroff")
    cam = sess.cam
    res = await cam.poweroff()
    res["quiesce"] = rep
    if not str(res.get("result", "")).startswith("ok"):
        # Refused (e.g. wedged): the session lost its event subscription, so
        # reconnect from scratch; the ready hook then resumes the paused work.
        cam.log_event("warn", "poweroff -> %s; reconnecting to resume" % res.get("result"))
        cam.request_reconnect()
    # Accepted: the keepalive notices the camera going away within a few seconds;
    # when it is switched on again (by hand / BLE wake) the ready hook resumes.
    return res
