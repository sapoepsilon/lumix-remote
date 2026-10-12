"""Generic settings model built from ``getinfo&type=allmenu`` + ``curmenu``.

allmenu is the camera's own menu tree: every selectable leaf carries the exact
``cmd_mode``/``cmd_type``/``cmd_value``(/``cmd_value2``) to send, and a
``title_id`` resolved through the ``<titlelist>`` (11 languages). curmenu lists
every item id with ``enable="yes|no"`` and, on parent items, the current
``value``. Together they describe every setting the camera exposes remotely,
so the UI can render all of them without a hard-coded list.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

PLACEHOLDER = "__value__"
SECTIONS = ["record_top", "record_qmenu", "record_menu", "record_tab", "record_tab2",
            "play_menu", "home_menu"]


@dataclass
class Option:
    id: str
    title: str
    mode: str
    type: str
    value: Optional[str]
    value2: Optional[str]
    enabled: bool = True

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "type": self.type, "value": self.value,
                "value2": self.value2, "enabled": self.enabled}


@dataclass
class Setting:
    id: str
    title: str
    section: str
    func_type: str
    path: List[str] = field(default_factory=list)  # parent titles (submenus)
    options: List[Option] = field(default_factory=list)
    current: Optional[str] = None
    enabled: bool = True

    @property
    def cmd_types(self) -> List[str]:
        seen: List[str] = []
        for o in self.options:
            if o.type not in seen:
                seen.append(o.type)
        return seen

    def current_option(self) -> Optional[Option]:
        if self.current is None:
            return None
        for o in self.options:
            if o.value == self.current and o.value2 is None:
                return o
        for o in self.options:
            if o.value == self.current or o.value2 == self.current:
                return o
        return None

    def to_dict(self) -> dict:
        cur = self.current_option()
        return {
            "id": self.id, "title": self.title, "section": self.section, "path": self.path,
            "func_type": self.func_type, "cmd_types": self.cmd_types, "enabled": self.enabled,
            "current": self.current, "current_option": cur.id if cur else None,
            "current_title": cur.title if cur else self.current,
            "options": [o.to_dict() for o in self.options],
        }


class MenuModel:
    def __init__(self, allmenu_xml: bytes, lang: str = "en"):
        root = ET.fromstring(allmenu_xml)
        menuset = root.find("menuset")
        if menuset is None:
            raise ValueError("allmenu reply has no <menuset>")
        self.model = menuset.get("model", "")
        self.version = menuset.get("version", "")
        self.titles = self._titles(menuset, lang)
        self.settings: Dict[str, Setting] = {}
        self.order: List[str] = []
        for sec in SECTIONS:
            el = menuset.find(sec)
            if el is not None:
                self._walk(el, sec, [])
        self.enable: Dict[str, bool] = {}

    @staticmethod
    def _titles(menuset: ET.Element, lang: str) -> Dict[str, str]:
        tl = menuset.find("titlelist")
        out: Dict[str, str] = {}
        if tl is None:
            return out
        chosen = None
        for l in tl.findall("language"):
            if l.get("code") == lang:
                chosen = l
                break
            if chosen is None and l.get("default") == "yes":
                chosen = l
        if chosen is not None:
            for t in chosen.findall("title"):
                out[t.get("id", "")] = (t.text or "").strip()
        return out

    def title(self, el: ET.Element) -> str:
        tid = el.get("title_id", "")
        if tid in self.titles and self.titles[tid]:
            return self.titles[tid]
        iid = el.get("id", "")
        return iid.replace("menu_item_id_", "").replace("_", " ")

    def _walk(self, parent: ET.Element, section: str, path: List[str]) -> None:
        for item in _child_items(parent):
            opts = [o for o in _child_items(item) if o.get("cmd_mode")]
            if opts:
                sid = item.get("id", "")
                if sid not in self.settings:
                    s = Setting(id=sid, title=self.title(item), section=section,
                                func_type=item.get("func_type", ""), path=list(path))
                    for o in opts:
                        v, v2 = o.get("cmd_value"), o.get("cmd_value2")
                        s.options.append(Option(
                            id=o.get("id", ""), title=self.title(o), mode=o.get("cmd_mode", ""),
                            type=o.get("cmd_type", ""),
                            value=None if v == PLACEHOLDER else v,
                            value2=None if v2 == PLACEHOLDER else v2))
                    self.settings[sid] = s
                    self.order.append(sid)
            # Options can have their own sub-groups (e.g. recmode > creative movie > P/A/S/M),
            # and submenus nest further settings.
            self._walk(item, section, path + [self.title(item)])

    def apply_curmenu(self, curmenu_xml: bytes) -> List[str]:
        """Update current values/enabled flags. Returns ids of settings that changed."""
        root = ET.fromstring(curmenu_xml)
        values: Dict[str, Optional[str]] = {}
        enable: Dict[str, bool] = {}
        for it in root.iter("item"):
            iid = it.get("id", "")
            enable[iid] = it.get("enable", "yes") == "yes"
            if it.get("value") is not None:
                values[iid] = it.get("value")
        changed: List[str] = []
        for sid, s in self.settings.items():
            before = (s.current, s.enabled, tuple(o.enabled for o in s.options))
            s.current = values.get(sid)
            s.enabled = enable.get(sid, True)
            for o in s.options:
                o.enabled = enable.get(o.id, True)
            if before != (s.current, s.enabled, tuple(o.enabled for o in s.options)):
                changed.append(sid)
        self.enable = enable
        return changed

    def by_cmd_type(self, cmd_type: str) -> Optional[Setting]:
        """Best setting whose options all use cmd_type (prefers ones with a current value)."""
        best = None
        for sid in self.order:
            s = self.settings[sid]
            if s.cmd_types == [cmd_type]:
                if s.current is not None and s.enabled:
                    return s
                if best is None:
                    best = s
        return best

    def all(self) -> Iterable[Setting]:
        return (self.settings[sid] for sid in self.order)


def _child_items(el: ET.Element) -> List[ET.Element]:
    """Direct <item> children, looking through <menu>/<group> wrappers."""
    out: List[ET.Element] = []
    for c in el:
        if c.tag == "item":
            out.append(c)
        elif c.tag in ("menu", "group"):
            out.extend(_child_items(c))
    return out
