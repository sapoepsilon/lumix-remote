"""Map friendly names typed on the command line to the server's menu settings.

A setting can be named by its full id (``menu_item_id_sensitivity``), its short
name (``sensitivity``), the camera command type it drives (``iso``) or its title
(``"Exp. Comp"``, case/space-insensitive). An option can be named by its short
token (``auto``, ``L50``), full id, camera value (``-14/3``) or title (``"+1/3"``).
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Optional

PREFIX = "menu_item_id_"

# Pseudo settings backed by raw setsetting focal/shtrspeed (exposure dial values).
EXPOSURE = {"aperture": "focal", "f": "focal", "focal": "focal",
            "shutter": "shtrspeed", "ss": "shtrspeed", "shtrspeed": "shtrspeed"}


class ResolveError(ValueError):
    pass


def norm(s: Any) -> str:
    return re.sub(r"[\s_.\-()]+", "", str(s)).casefold()


def short_id(sid: str) -> str:
    return sid[len(PREFIX):] if sid.startswith(PREFIX) else sid


def option_tokens(setting: Dict[str, Any]) -> List[str]:
    """Shell-safe token per option: the option id minus the common prefix."""
    ids = [o["id"] for o in setting.get("options", [])]
    if not ids:
        return []
    pre = os.path.commonprefix(ids) if len(ids) > 1 else ""
    if len(ids) == 1:
        pre = PREFIX + short_id(setting["id"]) + "_"
        if not ids[0].startswith(pre):
            pre = PREFIX
    # cut back to the last "_" so tokens start at a word boundary
    cut = pre.rfind("_") + 1
    toks = [i[cut:] or i for i in ids]
    return toks


def _unique(cands: List[Dict[str, Any]], what: str, name: str) -> Optional[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    for c in cands:
        seen.setdefault(c["id"], c)
    if len(seen) == 1:
        return next(iter(seen.values()))
    if len(seen) > 1:
        raise ResolveError("%s %r is ambiguous: %s" % (what, name, ", ".join(short_id(i) for i in seen)))
    return None


def find_setting(settings: Iterable[Dict[str, Any]], name: str) -> Dict[str, Any]:
    settings = list(settings)
    n = norm(name)
    for rule in (
        lambda s: s["id"] == name,
        lambda s: short_id(s["id"]) == name,
        lambda s: norm(short_id(s["id"])) == n,
        lambda s: any(norm(t) == n for t in s.get("cmd_types", [])) and s.get("enabled", True),
        lambda s: any(norm(t) == n for t in s.get("cmd_types", [])),
        lambda s: norm(s.get("title", "")) == n,
    ):
        hit = _unique([s for s in settings if rule(s)], "setting", name)
        if hit is not None:
            return hit
    close = [short_id(s["id"]) for s in settings if n and n in norm(short_id(s["id"])) + norm(s.get("title", ""))]
    hint = ("; did you mean: %s" % ", ".join(close[:8])) if close else "; see `lumix settings list`"
    raise ResolveError("unknown setting %r%s" % (name, hint))


def find_option(setting: Dict[str, Any], name: str) -> Dict[str, Any]:
    opts = setting.get("options", [])
    toks = option_tokens(setting)
    n = norm(name)
    for rule in (
        lambda i, o: o["id"] == name,
        lambda i, o: toks[i] == name,
        lambda i, o: o.get("value") == name,
        lambda i, o: norm(toks[i]) == n,
        lambda i, o: norm(o.get("title", "")) == n,
        lambda i, o: norm(o.get("value") or "") == n,
    ):
        hit = _unique([o for i, o in enumerate(opts) if rule(i, o)], "option", name)
        if hit is not None:
            return hit
    raise ResolveError("%r is not an option of %s; allowed: %s"
                       % (name, short_id(setting["id"]), ", ".join(toks)))


def find_exposure(options: List[Dict[str, str]], name: str, kind: str) -> Dict[str, str]:
    """Aperture/shutter option by code (1024/256), title (F4.0, 1/250) or bare number (4, 250)."""
    n = norm(name)
    cands = [n]
    if kind == "focal":
        cands.append(norm("f" + name))
        try:
            cands.append(norm("F%.1f" % float(name.lstrip("fF"))))
        except ValueError:
            pass
    else:
        if re.fullmatch(r"\d+", name):
            cands.append(norm("1/" + name))
        if name.endswith("s"):
            cands.append(norm(name[:-1] + '"'))
        cands.append(norm(name + '"'))
    for o in options:
        if o["value"] == name:
            return o
    for c in cands:
        for o in options:
            if norm(o["title"]) == c:
                return o
    raise ResolveError("%r is not a valid %s; allowed: %s" % (
        name, "aperture" if kind == "focal" else "shutter speed",
        " ".join(o["title"] for o in options) or "(lens limits unknown)"))


def parse_coord(s: str) -> int:
    """0..1000 live-view coordinate from '500', '50%' or '0.5'."""
    s = s.strip()
    try:
        if s.endswith("%"):
            v = float(s[:-1]) * 10
        elif "." in s:
            f = float(s)
            v = f * 1000 if 0 <= f <= 1 else f
        else:
            v = float(s)
    except ValueError:
        raise ResolveError("bad coordinate %r (use 0..1000, 0.0..1.0 or N%%)" % s)
    if not 0 <= v <= 1000:
        raise ResolveError("coordinate %r out of range (0..1000 or 0..100%%)" % s)
    return int(round(v))
