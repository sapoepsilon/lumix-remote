# Camera models: profiles and how to add one

Everything that differs between Lumix bodies lives in a **model profile**
(`lumix/models/`). The rest of the code (connect sequence, live view, Auto
preset, BLE, onboarding UI) reads the profile instead of hard-coding the S5.

| File | What |
|---|---|
| `lumix/models/base.py` | `ModelProfile` and its parts: `ConnectQuirks`, `LiveViewRules`, `MenuAssist` |
| `lumix/models/dc_s5.py` | DC-S5 (firmware 2.03), verified on a real body |
| `lumix/models/generic.py` | Fallback for any other Lumix that answers `ddd` on :60606 |
| `lumix/models/__init__.py` | Registry: `for_ddd()`, `resolve()`, `get()`, `catalog()`, `register()` |

## How a profile is chosen

1. A camera's registry entry has `"model": "auto"` (default) or a profile id
   (`dc_s5`, `generic`, ...). The UI's model picker, `lumix cameras add --model`
   and `PATCH /api/cameras/{id} {"model": ...}` set it; `./run.sh --model` overrides it for one run.
2. With `auto`, the server reads `modelNumber` from the camera's
   `http://<ip>:60606/Lumix/Server0/ddd` at every connect and takes the profile
   that lists it in `model_numbers` (exact, case-insensitive). No match = `generic`.
3. `GET /api/cameras/{id}` shows the profile in use (`profile`), the status
   (`model`, `model_name`) and the event log says
   `DC-S5 S5-XXXXXX (fw 2.03): model profile dc_s5`.

## What a profile holds

| Field | Used by | S5 value |
|---|---|---|
| `handshake` | `Camera._access_values` | `req_acc` (plain). `auto` = LUMIX Sync's rule: `X_CamCapability` ending in `1` means encrypted `req_acc_g` (nonce) + `req_acc_e`; `handshake_fallback` drops back to plain `req_acc` when `req_acc_g` is refused |
| `connect.subscribe_only_in_play` | `_subscribe_safely` | yes: a SUBSCRIBE in rec makes the S5 switch itself to play (wedge trigger) |
| `connect.settle_event` / `settle_s` | `_connect` step 5 | wait up to 30 s for `mod_Play` after SUBSCRIBE before any camcmd |
| `connect.renew_by_sid_only` | supervisor | yes; a refused renewal tears the session down |
| `connect.recmode_only_if_needed`, `stopstream_only_if_running`, `lens_atta_restart_only_after_busy` | live view start/stop, NOTIFY handling | all yes (each prevents a known wedge) |
| `connect.poweroff_needs_rec` | BLE power-cycle | yes (`poweroff` = `err_reject` in play) |
| `connect.wedge_busy_streak`, `recmode_refused_from_play_is_wedge`, `busy_budget_exhausted_is_wedge` | wedge detection | 10 / yes / yes |
| `connect.wedge_triggers` | docs, `GET /api/models/{id}` | the list of things never to send |
| `capabilities` | UI hints | touch AF (type 5), dual SD, no power zoom, writable rec mode, no Kelvin WB, BLE wake but no BLE power-off |
| `capabilities.interval_native` (+ `interval_max_count`, `interval_min_s`) | timelapse `native` engine (`camctrl interval`), UI "In-camera" switch | yes, 9999, 1 s (verified on the body 2026-10-12: Time Lapse drive + counts, then the shutter starts the run; `camctrl interval set` is rejected once armed and not needed). Generic: no |
| `liveview` | `lumix.liveview.extract_jpeg` | JPEG at `be16(data[30:32]) + 32`, SOI search fallback |
| `menu_assist` | UI "Tap in MF" default, "Close MF Assist inset" | MF tap opens the PIP inset, not switchable remotely, closed by flipping focus to AF-S and back |
| `auto_preset`, `auto_saved_types` | `lumix.presets.AutoPreset` | iA (fallback Program AE / Creative Video P), AF-S/AF-C, auto-detect AF, ISO auto, AWB, EV 0, multi metering, single drive |
| `ble` | BLE helper, docs | GATT UUID map, advertised name pattern, forbidden remote-shutter characteristic |
| `setup_wifi`, `setup_ble`, `approve_hint` | onboarding wizard, connect screen, `GET /api/connection` | exact S5 menu paths |

## Adding a model

1. Get the facts with the camera (or ask a user who has one): the `ddd` XML
   (`curl http://<ip>:60606/Lumix/Server0/ddd`), `getinfo capability`, and a
   server log from a connect with the `generic` profile
   (`./run.sh --camera test --model generic -v`, log in `logs/camera-test-*.log`).
   Strip serial numbers, MAC addresses and SSIDs before committing anything.
2. Copy the template below to `lumix/models/<id>.py` (id = model number in
   lower case with `_`, e.g. `dc_g9m2`). Start from `S5.derive(...)` when the body
   behaves like the S5, or from `GENERIC.derive(...)` when unsure.
3. Register it in `lumix/models/__init__.py`: import it and add it to
   `PROFILES` before `GENERIC`.
4. Fill `setup_wifi` / `setup_ble` with the camera's own menu names (the wizard
   shows them verbatim, with `detail` as the grey second line).
5. Keep `verified=False` until someone ran it end to end on the body (connect,
   live view, capture, Auto on/off, files). The UI marks unverified profiles.
6. Check selection by `modelNumber` and the handshake on the real body (`scripts/real-camera-smoke.sh`).

```python
"""Panasonic Lumix DC-XXXX (firmware x.y). Sources: ..."""
from __future__ import annotations

from .base import ConnectQuirks, LiveViewRules, MenuAssist, ModelProfile
from .dc_s5 import S5

XXXX = S5.derive(
    id="dc_xxxx",
    name="Lumix XXXX",
    model_numbers=["DC-XXXX"],
    verified=False,
    notes="What was tested, what differs from the S5.",
    handshake="auto",                       # or "req_acc" / "req_acc_g"
    connect=ConnectQuirks(settle_event="mod_Play", settle_s=30.0),
    capabilities={"touch_af": True, "dual_sd": False, "power_zoom": False, "files": True},
    liveview=LiveViewRules(),               # change only if frames don't decode
    menu_assist=MenuAssist(),
    setup_wifi=[
        {"title": "MENU → Setup → Wi-Fi", "detail": "optional second line"},
        {"title": "Wi-Fi Function → New Connection"},
        {"title": "Remote Shooting & View"},
        {"title": "Via Network → your home Wi-Fi"},
    ],
    setup_ble=[],                           # empty = the wizard skips Bluetooth
    ble=None,                               # GATT map if the body is known to work over BLE
)
```

`derive()` deep-copies, so changing a nested value never touches the S5 profile.
