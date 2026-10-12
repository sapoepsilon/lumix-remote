"""Panasonic Lumix DC-S5 (firmware 2.03), verified on a real body.

Findings behind every value: probe-results/FINDINGS.md, docs/lumix-sync-wifi-flow.md,
docs/bluetooth-research.md.
"""

from __future__ import annotations

from .. import ble as _ble   # GATT constants only; bleak is imported lazily there
from .base import ConnectQuirks, LiveViewRules, MenuAssist, ModelProfile

COMMON = [
    ("afmode", "auto_recognize", "animal_off"),
    ("iso", "auto", None),
    ("whitebalance", "auto", None),
    ("exposure", "0", None),
    ("lightmetering", "multi", None),
]

S5 = ModelProfile(
    id="dc_s5",
    name="Lumix S5",
    model_numbers=["DC-S5"],
    verified=True,
    notes="Firmware 2.03. Plain req_acc (no X_CamCapability; req_acc_g answers err_param). "
          "Live view 640x360 at ~26 fps.",
    handshake="req_acc",
    connect=ConnectQuirks(
        subscribe_only_in_play=True,
        settle_event="mod_Play",
        settle_s=30.0,
        renew_by_sid_only=True,
        recmode_only_if_needed=True,
        stopstream_only_if_running=True,
        lens_atta_restart_only_after_busy=True,
        poweroff_needs_rec=True,
        wedge_busy_streak=10,
        recmode_refused_from_play_is_wedge=True,
        raw_img_send=True,
        wedge_triggers=[
            "UPnP SUBSCRIBE while the camera is in rec mode, then recmode during its own switch to play",
            "camcmd/stopstream within ~30 s after SUBSCRIBE, before the mod_Play event",
            "recmode churn (restarting live view on every lens_Atta echo)",
            "stopstream with no stream running (err_critical, seen right before a wedge)",
            "BLE remote-shutter characteristic 7be5fd56 (turns the camera's Wi-Fi off)",
            "camcmd playmode/recmode while an in-camera interval run is going (paused: playmode ok, "
            "then recmode err_critical, 2026-10-12 11:55); also setsetting into it",
        ],
    ),
    capabilities={
        "touch_af": True, "touch_type": "5", "dual_sd": True, "power_zoom": False, "files": True,
        "mode_dial_writable": True, "kelvin_wb": False, "raw_send": True, "ble_wake": True,
        "ble_poweroff": False, "liveview_size": "640x360",
        # In-camera interval shooting over Wi-Fi (LUMIX Sync's camctrl interval flow,
        # docs/lumix-sync-wifi-flow.md section 10). The command set is from the APK
        # and the emulator; on the body only the menu values were seen so far.
        "interval_native": True, "interval_max_count": 9999, "interval_min_s": 1,
    },
    liveview=LiveViewRules(header_len_offset=30, header_len_add=32, soi_search_fallback=True,
                           min_datagram=64, mac_bytes=(9, 15), default_quality="vga"),
    menu_assist=MenuAssist(tap_in_mf_opens_inset=True, remote_toggle=False,
                           close_by_focus_flip="afs", ui_default_mf_tap="off"),
    auto_preset={
        "photo": {"recmode": [("recmode", "ia", None), ("recmode", "program_ae", None)],
                  "steps": [("drivemode", "normal", None), ("focusmode", "afs", None)] + COMMON},
        "video": {"recmode": [("recmode", "ia", None), ("recmode", "creative_movie", "program_ae")],
                  "steps": [("drivemode", "normal", None), ("focusmode", "afc", None)] + COMMON},
    },
    # mode first (it reloads per-mode settings); afmode before focusmode (locked in MF);
    # aperture/shutter last
    auto_saved_types=["recmode", "drivemode", "afmode", "focusmode", "iso", "whitebalance",
                      "exposure", "lightmetering", "focal", "shtrspeed"],
    ble={
        "name_pattern": _ble.NAME_RE.pattern,        # advertised name, e.g. S5-XXXXXX
        "company_id": _ble.PANASONIC_CID,
        "service_main": _ble.SVC_MAIN,
        "client_uuid": _ble.CH_DLNA_UUID,
        "friendly_name": _ble.CH_FRIENDLY_NAME,
        "initial_inform": _ble.CH_INITIAL_INFORM,
        "camera_ap_ssid": _ble.CH_CAMERA_SSID,
        "camera_ap_pass": _ble.CH_CAMERA_AP_PASS,
        "camera_sta_ssid": _ble.CH_CAMERA_STA_SSID,
        "wifi_start": _ble.CH_WIFI_START,
        "wifi_cmd": _ble.CH_WIFI_CMD,
        "wifi_ssid": _ble.CH_WIFI_SSID,
        "clock": _ble.CH_CLOCK,
        "state_notify": _ble.CH_STATE_NOTIFY,
        "forbidden": ["7be5fd56-475b-11e7-a919-92ebcb67fe33"],   # remote shutter: kills Wi-Fi
        "registration": "client UUID + name over BLE, then accctrl on the camera's own AP",
    },
    setup_wifi=[
        {"title": "Press MENU/SET → Setup (wrench) → Wi-Fi",
         "detail": "Setup tab, page with Wi-Fi and Bluetooth."},
        {"title": "Wi-Fi Function → New Connection"},
        {"title": "Remote Shooting & View"},
        {"title": "Press DISP to switch to \"Via Network\"",
         "detail": "The camera then lists Wi-Fi networks instead of starting its own."},
        {"title": "Pick your home Wi-Fi (the same network as this computer)",
         "detail": "Enter its password on the camera the first time."},
        {"title": "Wait for \"Waiting for connection\" / the camera's SSID screen",
         "detail": "Leave the camera on this screen; this app finds it on the network."},
    ],
    setup_ble=[
        {"title": "Press MENU/SET → Setup (wrench) → Bluetooth"},
        {"title": "Bluetooth → ON"},
        {"title": "Bluetooth → SET → Pairing",
         "detail": "The camera now advertises itself for pairing."},
        {"title": "Remote Wakeup → ON",
         "detail": "Lets this computer switch the camera's Wi-Fi on over Bluetooth."},
        {"title": "Close LUMIX Sync on your phone",
         "detail": "The camera takes one Bluetooth controller at a time."},
    ],
    approve_hint="The S5 usually registers this computer by itself (\"ok_under_research_no_msg\"). "
                 "If it shows a prompt, press OK on the camera.",
)
