"""One-tap presets that change several camera settings and can be undone.

``AutoPreset`` puts the camera into "let the camera decide" mode:

* Exposure mode: Intelligent Auto (``setsetting recmode ia``). On the DC-S5 the
  rec mode *is* writable over Wi-Fi even though the body has a mode dial; the
  setting sticks until somebody turns the dial. If a body rejects it we fall
  back to Program AE (photo) / Creative Video P (video).
* Focus: AF-S for photos, AF-C for video, AF mode "Auto-detection" (face/eye/
  body; ignored by the camera while iA forces it anyway). Using AF also keeps
  the camera out of MF Assist, whose magnified picture-in-picture shows up in
  the live view whenever focus is MF and the screen is touched.
* ISO auto, auto white balance, exposure compensation 0, multi metering,
  single-shot drive (a time-lapse drive mode would turn the shutter button
  into "start time lapse").

Before the first apply, the current value of every touched setting is read
with ``getsetting`` (value plus ``value2`` from the element text) and stored in
``<config>/presets.json``; turning Auto off writes them back in a safe order
(rec mode first, because changing the mode reloads the mode-specific
settings), so manual values survive server restarts too. Picture quality,
size and video quality are left alone: they are the user's storage choices,
not exposure decisions.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from .camera import CONFIG_DIR, Camera

Step = Tuple[str, str, Optional[str]]

COMMON: List[Step] = [
    ("afmode", "auto_recognize", "animal_off"),
    ("iso", "auto", None),
    ("whitebalance", "auto", None),
    ("exposure", "0", None),
    ("lightmetering", "multi", None),
]
AUTO: Dict[str, Dict[str, Any]] = {
    "photo": {
        "recmode": [("recmode", "ia", None), ("recmode", "program_ae", None)],
        "steps": [("drivemode", "normal", None), ("focusmode", "afs", None)] + COMMON,
    },
    "video": {
        "recmode": [("recmode", "ia", None), ("recmode", "creative_movie", "program_ae")],
        "steps": [("drivemode", "normal", None), ("focusmode", "afc", None)] + COMMON,
    },
}
# Restore order: mode first (it reloads per-mode settings), aperture/shutter last.
# afmode before focusmode: AF mode is locked while focus is MF.
SAVED_TYPES = ["recmode", "drivemode", "afmode", "focusmode", "iso", "whitebalance",
               "exposure", "lightmetering", "focal", "shtrspeed"]


class AutoPreset:
    def __init__(self, cam: Camera, path: Optional[str] = None):
        self.cam = cam
        self.path = path or os.path.join(CONFIG_DIR, "presets.json")
        self.state: Dict[str, Any] = self._load()
        # One preset change at a time: an "on" racing an "off" must not drop
        # the saved manual values.
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- state
    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path) as f:
                d = json.load(f)
            return d.get("auto", {}) if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"auto": self.state}, f, indent=1)
        os.replace(tmp, self.path)

    def to_dict(self) -> Dict[str, Any]:
        return {"on": bool(self.state.get("on")), "target": self.state.get("target"),
                "saved": self.state.get("saved"), "applied": self.state.get("applied"),
                "since": self.state.get("since"), "recmode_used": self.state.get("recmode_used")}

    # ------------------------------------------------------------- camera io
    @staticmethod
    async def _get(t, cmd_type: str) -> Optional[List[Optional[str]]]:
        r = await t.cgi("getsetting", cmd_type)
        if not r.ok or r.xml is None:
            return None
        el = r.xml.find("settingvalue")
        if el is None or el.get(cmd_type) is None:
            return None
        v2 = (el.text or "").strip() or None
        return [el.get(cmd_type), v2]

    @staticmethod
    async def _set(t, cmd_type: str, value: str, value2: Optional[str]) -> str:
        r = await t.cgi("setsetting", cmd_type, value, value2)
        return r.result

    async def snapshot(self) -> Dict[str, List[Optional[str]]]:
        out: Dict[str, List[Optional[str]]] = {}
        async with self.cam.exclusive("preset") as t:
            for ct in SAVED_TYPES:
                v = await self._get(t, ct)
                if v is None and ct == "recmode":
                    await self.cam.refresh_menu()
                    v = self._menu_recmode()
                if v is not None:
                    out[ct] = v
        return out

    def _menu_recmode(self) -> Optional[List[Optional[str]]]:
        """The S5 answers err_critical to getsetting recmode; curmenu has it."""
        menu = self.cam.menu
        s = menu.by_cmd_type("recmode") if menu else None
        if s is None or s.current is None:
            return None
        # Sub-modes (Creative Video P/A/S/M, C3-n) live in a nested setting.
        for sub in menu.all():
            if sub is not s and sub.cmd_types == ["recmode"] and sub.current is not None:
                if any(o.value == s.current and o.value2 is not None for o in sub.options):
                    return [s.current, sub.current]
        return [s.current, None]

    # --------------------------------------------------------------- actions
    async def set(self, on: bool, target: str = "photo") -> Dict[str, Any]:
        if target not in AUTO:
            raise ValueError("target must be photo or video")
        async with self._lock:
            if on:
                return await self._apply(target)
            return await self._restore()

    async def _apply(self, target: str) -> Dict[str, Any]:
        cam = self.cam
        if not self.state.get("on") or not self.state.get("saved"):
            # Only snapshot manual values when coming from manual: re-applying
            # (or photo -> video) must not overwrite them with auto values.
            self.state = {"saved": await self.snapshot()}
        before = await self.snapshot()
        plan = AUTO[target]
        results: List[Dict[str, Any]] = []
        async with cam.exclusive("preset") as t:
            used = None
            for ct, v, v2 in plan["recmode"]:
                res = await self._set(t, ct, v, v2)
                results.append({"type": ct, "value": v, "value2": v2, "result": res})
                if res == "ok":
                    used = v if v2 is None else "%s/%s" % (v, v2)
                    break
            for ct, v, v2 in plan["steps"]:
                res = await self._set(t, ct, v, v2)
                results.append({"type": ct, "value": v, "value2": v2, "result": res})
        await cam.refresh_menu()
        after = await self.snapshot()
        self.state.update({"on": True, "target": target, "since": time.time(),
                           "recmode_used": used, "applied": results})
        self._save()
        cam.log_event("preset", "auto %s on (mode %s)" % (target, used))
        cam.bus.publish("preset", self.to_dict())
        return {"auto": self.to_dict(), "steps": results, "before": before, "after": after}

    async def _restore(self) -> Dict[str, Any]:
        cam = self.cam
        saved: Dict[str, List[Optional[str]]] = self.state.get("saved") or {}
        if not saved:
            self.state = {"on": False}
            self._save()
            return {"auto": self.to_dict(), "steps": [], "note": "nothing saved to restore"}
        before = await self.snapshot()
        results: List[Dict[str, Any]] = []
        async with cam.exclusive("preset") as t:
            for ct in SAVED_TYPES:
                if ct not in saved:
                    continue
                v, v2 = saved[ct]
                if v is None:
                    continue
                cur = before.get(ct)
                if cur is not None and cur[0] == v and (cur[1] == v2 or v2 is None):
                    results.append({"type": ct, "value": v, "value2": v2, "result": "unchanged"})
                    continue
                res = await self._set(t, ct, v, v2)
                if res != "ok" and v2 is not None:  # some types take no value2 on set
                    res2 = await self._set(t, ct, v, None)
                    res = res2 if res2 == "ok" else res
                results.append({"type": ct, "value": v, "value2": v2, "result": res})
        await cam.refresh_menu()
        await cam.refresh_exposure()
        after = await self.snapshot()
        self.state = {"on": False, "restored": saved, "restored_at": time.time()}
        self._save()
        cam.log_event("preset", "auto off, manual settings restored")
        cam.bus.publish("preset", self.to_dict())
        return {"auto": self.to_dict(), "steps": results, "before": before, "after": after,
                "restored": saved}
