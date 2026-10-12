"""Camera model profiles: everything that differs between Lumix bodies.

A ``ModelProfile`` is plain data. ``lumix.camera.Camera`` reads it for the
handshake, the connect sequence and the wedge guard; the live view receiver for
its datagram layout; ``lumix.presets`` for the Auto preset; the web UI for the
camera-side setup steps and MF Assist behaviour. Add a model by copying
``dc_s5.py`` (see docs/camera-models.md) and registering it in ``__init__``.

Everything here must stay stdlib-only and Python 3.9 compatible: the BLE helper
subprocess imports it too.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Auto preset plan step: (setsetting type, value, value2)
Step = Tuple[str, str, Optional[str]]

HANDSHAKES = ("auto", "req_acc", "req_acc_g")


@dataclass
class ConnectQuirks:
    """How careful the connect sequence has to be (defaults = LUMIX Sync's flow,
    which is safe on the S5; see docs/lumix-sync-wifi-flow.md)."""

    # Send UPnP SUBSCRIBE only while the camera is in play mode (a SUBSCRIBE in
    # rec makes the S5 switch itself to play, and a recmode during that switch
    # wedges camcmd until a power cycle).
    subscribe_only_in_play: bool = True
    # After SUBSCRIBE wait for this X_Panasonic_Cam_Sync event before any
    # camcmd/stopstream, at most settle_s seconds. None = only wait settle_min_s.
    settle_event: Optional[str] = "mod_Play"
    settle_s: float = 30.0
    settle_min_s: float = 0.2
    # Renew the GENA subscription by SID only; a refused renewal tears the
    # session down instead of re-subscribing on a live session.
    renew_by_sid_only: bool = True
    # Only send recmode when getstate says the camera is not in rec already.
    recmode_only_if_needed: bool = True
    # stopstream only for a stream we started (else err_critical on the S5).
    stopstream_only_if_running: bool = True
    # Restart live view on lens_Atta only after a camera "busy" window
    # (outside it lens_Atta is the echo of our own recmode: restart loop).
    lens_atta_restart_only_after_busy: bool = True
    # camcmd poweroff is refused (err_reject) in play mode.
    poweroff_needs_rec: bool = True
    # err_busy/err_critical answers to camcmd/startstream in a row that mean
    # "wedged" (cmd() retries 5x, so 10 = two commands' full retry budget).
    wedge_busy_streak: int = 10
    # A recmode refused from play for the whole retry budget = wedged at once.
    recmode_refused_from_play_is_wedge: bool = True
    # Any other camcmd/startstream that exhausts the whole err_busy/err_critical
    # retry budget (5 x 1 s) while the body is not reported busy = wedged at once.
    busy_budget_exhausted_is_wedge: bool = True
    # setsetting raw_img_send=enable at connect (LUMIX Sync's "send RAW").
    raw_img_send: bool = True
    # Things that are known to wedge the body; shown in docs/UI, never sent.
    wedge_triggers: List[str] = field(default_factory=list)


@dataclass
class LiveViewRules:
    """Where the JPEG sits in each live view UDP datagram."""

    # JPEG start = be16 at header_len_offset + header_len_add (GMaster rule; S5: 0x00de+32=254)
    header_len_offset: int = 30
    header_len_add: int = 32
    # Search for FF D8 FF when the computed offset does not hold a JPEG SOI.
    soi_search_fallback: bool = True
    min_datagram: int = 64
    # Bytes in the header that carry the camera MAC (informational).
    mac_bytes: Optional[Tuple[int, int]] = None
    # startstream value: the UDP port; the camera sends 16:9 640x360 "vga" by default.
    default_quality: str = "vga"


@dataclass
class MenuAssist:
    """Manual-focus assist (magnified PIP inset) behaviour."""

    tap_in_mf_opens_inset: bool = True       # a touch while focus is MF opens MF Assist
    remote_toggle: bool = False              # can MF Assist be switched off over Wi-Fi?
    close_by_focus_flip: Optional[str] = "afs"   # flip focusmode to this and back to close it
    ui_default_mf_tap: str = "off"           # UI default for "Tap in MF": "off" | "assist"


@dataclass
class ModelProfile:
    id: str                                   # registry key, e.g. "dc_s5"
    name: str                                 # human name, e.g. "Lumix S5"
    model_numbers: List[str] = field(default_factory=list)   # ddd modelNumber values (exact)
    verified: bool = False                    # tested on a real body
    notes: str = ""
    # "auto" decides from ddd X_CamCapability (ends in "1" = encrypted
    # req_acc_g/req_acc_e, LUMIX Sync's rule); "req_acc" = plain legacy
    # handshake; "req_acc_g" = always encrypted.
    handshake: str = "auto"
    # Fall back to plain req_acc when req_acc_g answers err_param / err_non_support.
    handshake_fallback: bool = True
    connect: ConnectQuirks = field(default_factory=ConnectQuirks)
    capabilities: Dict[str, Any] = field(default_factory=dict)
    liveview: LiveViewRules = field(default_factory=LiveViewRules)
    menu_assist: MenuAssist = field(default_factory=MenuAssist)
    # Auto preset: {"photo"|"video": {"recmode": [Step, ...] (first ok wins), "steps": [Step, ...]}}
    auto_preset: Dict[str, Dict[str, List[Step]]] = field(default_factory=dict)
    # Restore order for the Auto preset's saved manual values.
    auto_saved_types: List[str] = field(default_factory=list)
    # BLE: GATT map (name -> UUID) and advertised name pattern; None = no BLE support known.
    ble: Optional[Dict[str, Any]] = None
    # Camera-side onboarding steps (exact menu paths). Each step: {"title", "detail"?}.
    setup_wifi: List[Dict[str, str]] = field(default_factory=list)
    setup_ble: List[Dict[str, str]] = field(default_factory=list)
    approve_hint: str = "If the camera asks, press OK on the camera to allow this device."

    # ------------------------------------------------------------------ logic
    def matches(self, ddd: Dict[str, str]) -> bool:
        return (ddd.get("modelNumber") or "").strip().upper() in {m.upper() for m in self.model_numbers}

    def handshake_for(self, ddd: Dict[str, str]) -> str:
        """'req_acc' or 'req_acc_g' for this body."""
        if self.handshake != "auto":
            return self.handshake
        cap = (ddd.get("X_CamCapability") or "").strip()
        return "req_acc_g" if cap.endswith("1") else "req_acc"

    def auto_plan(self, target: str) -> Dict[str, List[Step]]:
        return self.auto_preset[target]

    def to_dict(self, full: bool = True) -> Dict[str, Any]:
        d = asdict(self)
        if not full:
            return {k: d[k] for k in ("id", "name", "model_numbers", "verified", "notes")}
        return d

    def derive(self, **changes: Any) -> "ModelProfile":
        """Copy with some fields replaced (for a new model based on this one)."""
        p = copy.deepcopy(self)
        for k, v in changes.items():
            if not hasattr(p, k):
                raise AttributeError(k)
            setattr(p, k, v)
        return p
