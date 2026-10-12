"""Model profile registry: pick a profile from a camera's ddd modelNumber.

    >>> from lumix import models
    >>> models.for_ddd({"modelNumber": "DC-S5"}).id
    'dc_s5'
    >>> models.for_ddd({"modelNumber": "DC-G9"}).id
    'generic'

To add a body: copy dc_s5.py, adjust, import it here and append it to PROFILES
(docs/camera-models.md walks through it).
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .base import ConnectQuirks, LiveViewRules, MenuAssist, ModelProfile
from .dc_s5 import S5
from .generic import GENERIC

AUTO = "auto"

# Order matters only for display; matching is exact on modelNumber.
PROFILES: List[ModelProfile] = [S5, GENERIC]
_BY_ID: Dict[str, ModelProfile] = {p.id: p for p in PROFILES}


def register(profile: ModelProfile) -> None:
    """Add a profile at runtime (plugins, tests). Replaces one with the same id."""
    _BY_ID[profile.id] = profile
    rest = [p for p in PROFILES if p.id != profile.id and p is not GENERIC]
    PROFILES[:] = rest + [profile] + ([GENERIC] if profile is not GENERIC else [])


def get(model_id: Optional[str]) -> Optional[ModelProfile]:
    return _BY_ID.get(model_id or "")


def for_ddd(ddd: Dict[str, str]) -> ModelProfile:
    """Profile for a device description; GENERIC when no profile claims the modelNumber."""
    for p in PROFILES:
        if p is not GENERIC and p.matches(ddd):
            return p
    return GENERIC


def resolve(pinned: Optional[str], ddd: Optional[Dict[str, str]]) -> ModelProfile:
    """The profile a camera uses: a pinned model id wins, else auto-detect from ddd."""
    if pinned and pinned != AUTO:
        p = get(pinned)
        if p is not None:
            return p
    return for_ddd(ddd or {})


def catalog() -> List[Dict[str, object]]:
    """Short list for pickers (UI, CLI)."""
    return [p.to_dict(full=False) for p in PROFILES]


__all__ = ["AUTO", "PROFILES", "ModelProfile", "ConnectQuirks", "LiveViewRules", "MenuAssist",
           "GENERIC", "S5", "register", "get", "for_ddd", "resolve", "catalog"]
