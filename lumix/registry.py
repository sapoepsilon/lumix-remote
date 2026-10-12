"""Per-camera configuration: ``~/.config/lumix-remote/cameras.json``.

One entry per camera the user set up (never committed; lives in the config
dir, ``LUMIX_REMOTE_HOME`` overrides it)::

    {"version": 1, "active": "s5",
     "cameras": [{"id": "s5", "nickname": "S5", "model": "auto" | "dc_s5" | ...,
                  "model_number": "DC-S5", "friendly_name": ..., "firmware": ...,
                  "last_ip": ..., "fixed_ip": null, "udn": "uuid:...",
                  "client_uuid": "4D454930-0100-1000-8001-...", "device_name": "LumixRemote",
                  "ble": {"name", "address", "identity", "home_ssid", "registered_at"},
                  "ports": {"http": 80, "upnp": 60606, "files": 50001, "liveview": 49199},
                  "settings": {"autoconnect": true, "watchdog": false},
                  "provisional": false, "added_at": ..., "connected_at": ...}]}

Per-camera files that change often (Auto preset state) live in
``cameras/<id>/``. Every write is a locked read-modify-write (``fcntl.flock``),
so the server and the BLE helper subprocess can both update the file.

Stdlib only and Python 3.9 compatible: the BLE helper (``.venv-ble``) and the
CLI import it too.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import shutil
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

VERSION = 1
FILENAME = "cameras.json"
DEFAULT_PORTS = {"http": 80, "upnp": 60606, "files": 50001, "liveview": 49199}
DEFAULT_SETTINGS = {"autoconnect": True, "watchdog": False}
LEGACY_FILES = ("state.json", "presets.json", "ble.json")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_BLE_NAME_RE = re.compile(r"^([A-Z0-9]{2,6})-[0-9A-F]{6}$")


class RegistryError(ValueError):
    pass


def config_dir() -> str:
    return os.path.expanduser(os.environ.get("LUMIX_REMOTE_HOME", "~/.config/lumix-remote"))


def pidfile_path() -> str:
    """The one server pidfile (written by the server, read by `lumix server`)."""
    return os.path.join(config_dir(), "server.pid")


def default_client_uuid() -> str:
    """LUMIX Sync-style client id: fixed MEI0 prefix + 12 hex chars of this machine."""
    return "4D454930-0100-1000-8001-%012X" % (uuid.getnode() & 0xFFFFFFFFFFFF)


def slugify(text: str, fallback: str = "camera") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:32].strip("-")
    return s or fallback


def new_entry(cam_id: str, nickname: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
    e: Dict[str, Any] = {
        "id": cam_id, "nickname": nickname or cam_id, "model": "auto",
        "model_number": None, "friendly_name": None, "firmware": None,
        "last_ip": None, "fixed_ip": None, "udn": None,
        "client_uuid": default_client_uuid(), "device_name": "LumixRemote",
        "ble": {"name": None, "address": None, "identity": None, "home_ssid": None,
                "registered_at": None},
        "ports": dict(DEFAULT_PORTS), "settings": dict(DEFAULT_SETTINGS),
        "provisional": True, "added_at": int(time.time()), "connected_at": None,
    }
    _merge(e, fields)
    return e


def _merge(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    """Shallow update, but nested dicts (ble, ports, settings) merge key by key."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            dst[k].update(v)
        else:
            dst[k] = v


def _normalize(entry: Dict[str, Any]) -> Dict[str, Any]:
    base = new_entry(entry.get("id") or "camera")
    _merge(base, entry)
    return base


class Registry:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(config_dir(), FILENAME)

    @property
    def dir(self) -> str:
        return os.path.dirname(self.path)

    def camera_dir(self, cam_id: str) -> str:
        return os.path.join(self.dir, "cameras", cam_id)

    # ------------------------------------------------------------- file io
    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        os.makedirs(self.dir, exist_ok=True)
        if fcntl is None:  # pragma: no cover
            yield
            return
        with open(self.path + ".lock", "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.path) as f:
                d = json.load(f)
        except FileNotFoundError:
            d = {}
        except (OSError, ValueError):
            # keep the broken file for the user, start fresh
            with contextlib.suppress(OSError):
                shutil.copy2(self.path, self.path + ".broken")
            d = {}
        if not isinstance(d, dict):
            d = {}
        cams = [_normalize(c) for c in d.get("cameras", []) if isinstance(c, dict) and c.get("id")]
        active = d.get("active")
        if active not in {c["id"] for c in cams}:
            active = cams[0]["id"] if cams else None
        return {"version": VERSION, "active": active, "cameras": cams}

    def _write(self, d: Dict[str, Any]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1, sort_keys=False)
            f.write("\n")
        os.replace(tmp, self.path)

    @contextlib.contextmanager
    def edit(self) -> Iterator[Dict[str, Any]]:
        """Locked read-modify-write of the whole file."""
        with self._locked():
            d = self._read()
            yield d
            self._write(d)

    def load(self) -> Dict[str, Any]:
        return self._read()

    def exists(self) -> bool:
        return os.path.exists(self.path)

    # ------------------------------------------------------------- queries
    def cameras(self) -> List[Dict[str, Any]]:
        return self._read()["cameras"]

    def active_id(self) -> Optional[str]:
        return self._read()["active"]

    def get(self, cam_id: str) -> Optional[Dict[str, Any]]:
        return next((c for c in self.cameras() if c["id"] == cam_id), None)

    def find(self, key: str) -> Optional[Dict[str, Any]]:
        """By id, then nickname (case-insensitive), then UDN."""
        cams = self.cameras()
        k = (key or "").strip()
        for pred in (lambda c: c["id"] == k,
                     lambda c: (c.get("nickname") or "").casefold() == k.casefold(),
                     lambda c: c.get("udn") and c["udn"] == k):
            hit = [c for c in cams if pred(c)]
            if len(hit) == 1:
                return hit[0]
        return None

    # ------------------------------------------------------------- changes
    def _unique_id(self, d: Dict[str, Any], want: str) -> str:
        taken = {c["id"] for c in d["cameras"]}
        if want not in taken:
            return want
        n = 2
        while "%s-%d" % (want, n) in taken:
            n += 1
        return "%s-%d" % (want, n)

    @staticmethod
    def _free_lv_port(d: Dict[str, Any], base: int = DEFAULT_PORTS["liveview"]) -> int:
        used = {int(c.get("ports", {}).get("liveview") or 0) for c in d["cameras"]}
        p = base
        while p in used:
            p += 1
        return p

    def add(self, nickname: Optional[str] = None, cam_id: Optional[str] = None,
            activate: Optional[bool] = None, **fields: Any) -> Dict[str, Any]:
        """New camera; first one becomes active. ``fields`` may hold model,
        fixed_ip, ports (partial), settings (partial), ble (partial), ..."""
        if cam_id is not None and not ID_RE.match(cam_id):
            raise RegistryError("camera id must be lowercase letters, digits, - or _ (max 32): %r" % cam_id)
        with self.edit() as d:
            cid = self._unique_id(d, cam_id or slugify(nickname or "camera"))
            ports = dict(fields.pop("ports", None) or {})
            ports.setdefault("liveview", self._free_lv_port(d))
            e = new_entry(cid, nickname or cid, ports=ports, **fields)
            d["cameras"].append(e)
            if activate or d["active"] is None:
                d["active"] = cid
        return e

    def update(self, cam_id: str, **fields: Any) -> Dict[str, Any]:
        with self.edit() as d:
            e = next((c for c in d["cameras"] if c["id"] == cam_id), None)
            if e is None:
                raise RegistryError("no camera %r" % cam_id)
            fields.pop("id", None)
            _merge(e, fields)
            out = copy.deepcopy(e)
        return out

    def rename(self, cam_id: str, nickname: str) -> Dict[str, Any]:
        nickname = (nickname or "").strip()
        if not nickname:
            raise RegistryError("nickname must not be empty")
        return self.update(cam_id, nickname=nickname[:64])

    def remove(self, cam_id: str) -> Dict[str, Any]:
        with self.edit() as d:
            e = next((c for c in d["cameras"] if c["id"] == cam_id), None)
            if e is None:
                raise RegistryError("no camera %r" % cam_id)
            d["cameras"] = [c for c in d["cameras"] if c["id"] != cam_id]
            if d["active"] == cam_id:
                d["active"] = d["cameras"][0]["id"] if d["cameras"] else None
        shutil.rmtree(self.camera_dir(cam_id), ignore_errors=True)
        return e

    def set_active(self, cam_id: str) -> None:
        with self.edit() as d:
            if cam_id not in {c["id"] for c in d["cameras"]}:
                raise RegistryError("no camera %r" % cam_id)
            d["active"] = cam_id


# ------------------------------------------------------------------ migration
def migrate(reg: Optional[Registry] = None) -> Optional[Dict[str, Any]]:
    """Fold the single-camera files of older versions into cameras.json.

    state.json (last_ip) + ble.json (camera_name, home_ssid, registered_at) become
    one camera entry; presets.json moves to cameras/<id>/presets.json. The old
    files are moved to legacy/ (not deleted). No-op when cameras.json exists or
    there is nothing to migrate. Returns a report or None.
    """
    reg = reg or Registry()
    if reg.exists():
        return None
    src = {n: os.path.join(reg.dir, n) for n in LEGACY_FILES}
    present = {n: p for n, p in src.items() if os.path.exists(p)}
    if not present:
        return None

    def load(name: str) -> Dict[str, Any]:
        try:
            with open(present[name]) as f:
                v = json.load(f)
            return v if isinstance(v, dict) else {}
        except (KeyError, OSError, ValueError):
            return {}

    state, ble = load("state.json"), load("ble.json")
    ble_name = ble.get("camera_name") if isinstance(ble.get("camera_name"), str) else None
    m = _BLE_NAME_RE.match(ble_name or "")
    nickname = m.group(1) if m else (ble_name or "Camera")
    fields: Dict[str, Any] = {
        "last_ip": state.get("last_ip"),
        "provisional": not state.get("last_ip"),
        "ble": {"name": ble_name, "home_ssid": ble.get("home_ssid"),
                "registered_at": ble.get("registered_at")},
        "settings": {"watchdog": os.environ.get("LUMIX_BLE_WATCHDOG", "") in ("1", "true", "yes", "on")},
    }
    e = reg.add(nickname, **fields)
    report: Dict[str, Any] = {"camera": e["id"], "moved": []}
    if "presets.json" in present:
        dst = os.path.join(reg.camera_dir(e["id"]), "presets.json")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(present["presets.json"], dst)
    legacy = os.path.join(reg.dir, "legacy")
    os.makedirs(legacy, exist_ok=True)
    for name, path in present.items():
        shutil.move(path, os.path.join(legacy, name))
        report["moved"].append(name)
    return report


def migrate_pidfile(legacy_paths: List[str]) -> Optional[str]:
    """Adopt a pidfile an older `lumix server start` wrote (logs/lumix-server.pid)
    into the unified pidfile, if that one is missing. Returns the adopted path."""
    dst = pidfile_path()
    for p in legacy_paths:
        if not os.path.exists(p):
            continue
        if not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with contextlib.suppress(OSError):
                shutil.copy2(p, dst)
        with contextlib.suppress(OSError):
            os.unlink(p)
        return p
    return None


# ------------------------------------------------------------------ camera store
class JsonStore:
    """Camera state in a single JSON file (stand-alone use, no registry)."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(config_dir(), "state.json")

    def load(self) -> Dict[str, Any]:
        try:
            with open(self.path) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, **kw: Any) -> None:
        d = self.load()
        d.update(kw)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, self.path)


class RegistryStore:
    """What a Camera remembers (last IP, identity) stored in its registry entry."""

    KEYS = ("last_ip", "udn", "model_number", "friendly_name", "firmware", "connected_at",
            "provisional")

    def __init__(self, reg: Registry, cam_id: str):
        self.reg, self.cam_id = reg, cam_id

    def load(self) -> Dict[str, Any]:
        e = self.reg.get(self.cam_id) or {}
        return {k: e.get(k) for k in self.KEYS}

    def save(self, **kw: Any) -> None:
        kw = {k: v for k, v in kw.items() if k in self.KEYS}
        if kw:
            with contextlib.suppress(RegistryError):   # camera removed meanwhile
                self.reg.update(self.cam_id, **kw)
