"""Fallback for any Panasonic Lumix that answers ddd on :60606.

Conservative: it uses LUMIX Sync's connect order and every S5 safety rule
(they cost a few seconds at connect and are harmless on bodies that don't
need them), decides the handshake from X_CamCapability like LUMIX Sync, and
keeps the Auto preset to the setting types every LUMIX Sync body knows.
"""

from __future__ import annotations

from .base import ConnectQuirks, LiveViewRules, MenuAssist, ModelProfile

COMMON = [
    ("iso", "auto", None),
    ("whitebalance", "auto", None),
    ("exposure", "0", None),
    ("lightmetering", "multi", None),
]

GENERIC = ModelProfile(
    id="generic",
    name="Other Panasonic Lumix",
    model_numbers=[],
    verified=False,
    notes="Any Lumix that speaks the LUMIX Sync / Image App cam.cgi protocol. Handshake chosen "
          "from X_CamCapability; S5 safety rules applied.",
    handshake="auto",
    connect=ConnectQuirks(),
    capabilities={
        # unknown until the camera answers getinfo capability; the UI reads the live values
        "touch_af": None, "dual_sd": None, "power_zoom": None, "files": True,
        "mode_dial_writable": None, "kelvin_wb": False, "ble_wake": None,
        "interval_native": False,      # server-driven timelapse only until verified
    },
    liveview=LiveViewRules(),
    menu_assist=MenuAssist(),
    auto_preset={
        "photo": {"recmode": [("recmode", "ia", None), ("recmode", "program_ae", None)],
                  "steps": [("drivemode", "normal", None), ("focusmode", "afs", None)] + COMMON},
        "video": {"recmode": [("recmode", "creative_movie", "program_ae"), ("recmode", "ia", None)],
                  "steps": [("focusmode", "afc", None)] + COMMON},
    },
    auto_saved_types=["recmode", "drivemode", "focusmode", "iso", "whitebalance", "exposure",
                      "lightmetering", "focal", "shtrspeed"],
    ble=None,
    setup_wifi=[
        {"title": "Open MENU → Setup (wrench) → Wi-Fi",
         "detail": "On older bodies the Wi-Fi button or Fn key may open the same menu."},
        {"title": "Wi-Fi Function → New Connection"},
        {"title": "Remote Shooting & View"},
        {"title": "Choose \"Via Network\" (not Direct)",
         "detail": "Lets the camera join your home Wi-Fi, the same network as this computer."},
        {"title": "Pick your home Wi-Fi network"},
        {"title": "Wait for the camera to show it is waiting for a device"},
    ],
    setup_ble=[],
)
