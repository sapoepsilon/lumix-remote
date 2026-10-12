# Lumix DC-S5 (fw 2.03) Bluetooth LE research

Compiled 2026-10-11. Tags: **[S5]** = observed on this camera, **[G9II]** = from Aikhjarto/LUMIX-G9II-Remote-Control (GPL-3.0, facts only, no code copied), **[manual]** = Panasonic S5 Operating Instructions, **[?]** = inference.

Raw artifacts: `probe-results/ble/` (scan1.json, gatt1.json, read1.json, read2.txt, idle1.txt, scan2.txt and the scripts that produced them). Venv: `.venv-ble` (Homebrew Python 3.14 + bleak 3.0.2; the system Python 3.9 can't build pyobjc).

No GATT writes, no notify subscriptions (those are CCCD writes), no pairing, nothing on the Wi-Fi API in this run.

## 1. What was confirmed on this S5

- **macOS BLE access works.** bleak (CoreBluetooth) scanned straight away from the terminal on macOS 26.6, with no permission prompt or error, so the terminal already has Bluetooth permission. A backend started some other way (launchd, a .app bundle) needs its own Bluetooth TCC grant (`NSBluetoothAlwaysUsageDescription`).
- **Advertisement [S5]** (camera on, Wi-Fi remote session active, Bluetooth on):
  - Local name `S5-XXXXXX` (same as the Wi-Fi device name / ddd friendlyName).
  - Manufacturer data, company 0x003A (Panasonic): `00 xx xx xx xx xx`. Likely a status byte followed by 5 bytes of the BT address (`xx:xx:xx:xx:xx:xx`, Panasonic OUI; the Wi-Fi MAC is ...xx:xx:xx). [?] The first byte is probably a state flag (on / off-standby / pairing); watch it as the camera's state changes.
  - Service UUID advertised: `054ac620-3214-11e6-ac0d-0002a5d5c51b`.
  - Connectable, RSSI about -40 dBm at desk distance. The macOS address is a per-host CoreBluetooth UUID (`00000000-0000-0000-0000-000000000000`), not the MAC. Match on name or manufacturer data, not on address.
- **Unauthenticated connect works, GATT discovery works**, MTU 185. With no traffic, an idle connection stayed up for 40 s and we closed it ourselves.
- **Any characteristic read drops the link.** Reading `054ac621-...-0001-...` (an info-looking char) and separately the suspected challenge char `03e23a31-...` both ended in `BleakError('disconnected')` within a few seconds. No bond was created (`system_profiler SPBluetoothDataType` lists nothing). Interpretation [?]: the characteristics need an encrypted, bonded link. CoreBluetooth reacts to "insufficient authentication/encryption" by starting pairing, and the camera, which is not in pairing standby and doesn't know the Mac, drops the connection. **Conclusion: the Mac must be bonded via the camera's Pairing menu before any GATT traffic.**

### GATT table [S5]
bleak handles on macOS are CoreBluetooth's own numbers, not ATT handles, and descriptors weren't reported. Address characteristics by UUID. Every vendor UUID ends `-0002a5d5c51b` (a 2016 time-based UUID family) except where noted.

| Service | Characteristic (props) |
|---|---|
| `054ac620-3214-11e6-ac0d-…` (advertised) | `03e23a31-a54c-40fa-a668-de9acdc910bb` (read), `8d08a420-3213-11e6-8aca` (write), `cd7a71a0-3213-11e6-8f56` (write), `69e4d998-54b7-40f5-a5f8-4cc236cd2347` (read), `e182ec40-3213-11e6-ab07` (write), `e182ec41` (notify), `e182ec42` (notify), `e206a5c0-3214-11e6-afe4` (read), `a09fad60-3215-11e6-bdc0` (read), `c97cf1a5-3c03-4290-8c1b-9e74b9500f54` (read), `e182ec43` (notify) |
| `1d74afe0-3214-11e6-8ab4-…` | `daff1bc0-3216-11e6-91c8` (write), `37701b80-32a7-11e6-bb83` (notify) |
| `34738720-3214-11e6-b66b-…` | `ead55e60-3216-11e6-a42e` (write), `4cf487c0-32a7-11e6-a50d` (notify) |
| `5ae57260-3214-11e6-979e-…` | `0d6f1880-3217-11e6-a4cb` (write), `18345be1-3217-11e6-b56c` (notify), `18345be0-3217-11e6-b56c` (write) |
| `6b005f20-3214-11e6-a5ac-…` | `48669da0-3217-11e6-b08d` (notify), `da989c59-2c9c-44a4-9cf0-0fd005a8973f` (notify), `e1392720-3215-11e6-a035` (read) |
| `7ddc40a0-3214-11e6-b4d0-…` | `76a2dbc0-3217-11e6-9cd7` (write), `f90c0fe0-3218-11e6-a3f3` (write), `0aad9ca0-3219-11e6-8222` (write), `20f5fffc-fa4d-43b6-be55-321b3df3687c` (read), `36d3228b-188c-474f-a262-e0f01c5a367d` (write), `1b7ff948-8e4d-43d7-8659-737e9540e231` (write), `7beca6bb-58fc-4b27-80c0-6ffc7f6ba4c8` (write), `3d22c6be-baa6-451b-aa2c-f96c50005910` (read) |
| `f3b05360-3215-11e6-8529-…` | `cb6b7580-3218-11e6-92ad` (write), `9ec00600-3217-11e6-a276` (write), `c73ed840-3217-11e6-ab63` (write), `d611b4a0-3217-11e6-8d98` (notify), `5d346ea0-3218-11e6-8038` (write), `9c781c60-3218-11e6-9932` (read), `da534d0a-63a3-447c-a889-aab701906af2` (notify) |
| `054ac620-3214-11e6-0001-…` | `054ac621/622/623-3214-11e6-0001` (read ×3; probably model / firmware / name) |
| `7be5faae-475b-11e7-a919-92ebcb67fe33` | `7be5fd56-…` (write), `7be5fe6e-…` (notify) (2017-era add-on) |

## 2. What is known from other bodies (G9II, LUMIX Sync) [G9II]

Aikhjarto captured LUMIX Sync (Android HCI snoop) against a G9II and addresses characteristics by Linux ATT value handle, not UUID. These are the LUMIX Sync facts, re-stated:

- **Login (challenge/response):** read 4 bytes from handle 0x002A (the challenge, call it N as big-endian uint32). Write 20 bytes to 0x002C = big-endian uint32 of N XOR each of `49454d10 10000130 02018000 450200a0 ffffff18`. Write 20 bytes to 0x002E = N XOR each of `35504603 00000000 00000000 00000000 ffffff00`. Afterwards notify 0x0046 = 01. Then the app writes the clock to 0x0044 and reads 0x0036 (camera name) and 0x0038. (Note `49454d10…` partly spells "IEM"/"5PF". These are app constants, so they are probably the same on all LUMIX Sync bodies, but that is unverified on the S5.)
- **Clock sync (Auto Clock Set):** write to 0x0044, 10 bytes little-endian: year u16, month, day, hour, minute, second, u8 0 (DST?), i16 UTC offset in minutes (example `e8070c170c170e003c00` = 2024-12-23 12:23:14, +60).
- **GPS (Location Logging):** LUMIX Sync writes 16 bytes to 0x003E several times per second: `<iiii` = (counter starting 0x5486AF20-ish, lon×1e7, lat×1e7, constant 4260249) (Aikhjarto's field order; partly guessed).
- **Wi-Fi:**
  - Camera AP mode: write 0x004A = 01, then 0x0030 = 05. Notifications 0x0088 = 02 and 0x0032 = 00 follow.
  - Join an existing network: write 32-byte zero-padded SSID to 0x004E, then 0x004A = 03 (notify 0x004C = 00), then 0x004A = 02, 0x0030 = 03 (notifies 0x0088 = 02, 0x0032 = 00). The password isn't sent over BLE: the camera must already know that network (it does: our S5 already joins 192.168.1.x in "Via Network" mode).
  - 5 GHz toggle: 0x00A0 = 02 (on) / 01 (off), G9II only.
- **Shutter:** write 0x0068 sequence 01, 02, 04 (half press, then full press), 05 (release). Video toggle: 06, 07. Notify 0x006A = 00 acks.
- **Remote wakeup:** nothing in Aikhjarto's code or notes. Per the manual, LUMIX Sync just connects over BLE and asks for Remote shooting, and the camera powers up and starts Wi-Fi. So "wake" is BLE connect + login + the Wi-Fi command above, sent while the switch is OFF [?].
- No other public reverse engineering of Lumix BLE was found (MRSa/A01d only has Olympus BLE power-on; pylumix, GMaster, libgphoto2 and liblumix are Wi-Fi only; GitHub and web searches for the UUIDs found nothing).

### Mapping G9II handles to S5 UUIDs [?]
The S5 service layout lines up structurally with the G9II handle sequence:
- Service `054ac620-…ac0d`: read `03e23a31` (challenge, 0x002A), write `8d08a420` and `cd7a71a0` (responses, 0x002C/0x002E), write `e182ec40` + notify `e182ec41` (Wi-Fi start, 0x0030/0x0032), reads `e206a5c0`/`a09fad60` (name, 0x0036/0x0038).
- `1d74afe0`: one write + one notify, probably GPS (0x003E).
- `34738720`: one write + one notify, probably clock (0x0044/0x0046).
- `5ae57260`: write + notify + write, probably Wi-Fi command / result / SSID (0x004A/0x004C/0x004E).
- `7ddc40a0` or `f3b05360`: probably shutter (0x0068/0x006A) and other settings.

This is a hypothesis. Confirm it with the read-back checks in section 5, or with an Android HCI snoop of LUMIX Sync against this S5.

## 3. User steps on the camera [manual]
Pairing (one-time). The camera shows no passkey (Just Works). Up to 16 devices can be registered, but only one can be connected at a time.
- MENU → Setup (wrench).
- Bluetooth → ON.
- Bluetooth → SET → Pairing.
- Camera shows "S5-XXXXXX", standby.
- Run our pair command.
- Approve the macOS prompt, if shown.
- Camera: "registration complete" → OK.

For wake and Wi-Fi via BLE:
- Bluetooth → Remote Wakeup → ON.
- Bluetooth → Auto Clock Set → ON (optional).
- Bluetooth → Location Logging → ON (optional).
- Wi-Fi network settings: home SSID saved (already done).
- Close LUMIX Sync on the phone (it grabs the single BLE link).
- Power switch OFF to test wake.

(The menu labels match the manual. The extracted manual text garbles the icon path, so check them on the camera.)

## 4. Proposed protocol for wake + Wi-Fi-on

1. Scan for name `^S5-` or Panasonic manufacturer data. Expect it to advertise while the switch is OFF only if Remote Wakeup is on [manual].
2. Connect (bonded, so encryption comes up automatically).
3. Read the challenge (`03e23a31`), write both responses (`8d08a420`, `cd7a71a0`) using the LUMIX Sync XOR constants, and wait for the login-ok notify.
4. Write the clock (Auto Clock Set).
5. Join the home network: write the SSID to the SSID char, then the "join" command, wait for its notify, then send the "start Wi-Fi" command. This should power up the camera from OFF.
6. Keep the BLE link (or drop it) and hand over to the existing Wi-Fi flow: TCP-scan :60606 or wait for the SSDP alive, then req_acc, SUBSCRIBE, recmode.
7. Shutter can also go over BLE as a fallback when Wi-Fi is down.

## 5. Next test plan (needs a quiet camera: no Wi-Fi agent, phone app closed)
1. Pair from the Mac (camera in Pairing standby). Check that a bond appears and the phone's pairing survives (camera keeps a registered list).
2. Read-only checks once bonded: read every readable char and log values (expect model `DC-S5`, firmware `2.03`, name `S5-XXXXXX`, and a changing 4-byte challenge).
3. Login: challenge/response. Success means a notify, and no disconnect after a few seconds.
4. Clock write (harmless, easy to verify on screen).
5. Wi-Fi join with the camera ON, then the same with the switch OFF and Remote Wakeup ON.
6. Measure standby advertising: how long the camera keeps advertising after switch-OFF, and battery % per hour with Remote Wakeup on.

## 6. Risks
- **Single link:** one BLE central at a time. If the phone's LUMIX Sync auto-reconnects, the Mac can't connect (and the reverse). Pairing the Mac does not remove the phone (16-slot list, oldest evicted only past 16) [manual].
- **Pairing may need app-level registration** during pairing mode (LUMIX Sync may write a device name or ID). If the bond succeeds but login fails, capture LUMIX Sync with an Android HCI snoop to see what it sends.
- **G9II-to-S5 differences:** the S5 (2020, LUMIX Sync gen 1) may use different constants or char order than the G9II (2023). The UUID mapping above is structural guesswork.
- **Battery:** with Remote Wakeup ON, Bluetooth stays powered while the switch is OFF and the battery drains [manual]. Panasonic gives no figure and no timeout. Measure it before relying on it, and consider a USB-C PD dummy battery for a fixed rig.
- **Writes to unknown chars:** the 7ddc40a0 / f3b05360 services have many write-only chars, possibly settings or firmware related. Only write chars whose purpose is confirmed.
- **macOS:** the CoreBluetooth address is per-host and can change after a Bluetooth reset or unpair. Pairing prompts appear as system dialogs. A launchd backend needs its own Bluetooth permission.
- **GPL:** Aikhjarto is GPL-3.0. Reimplement from these facts and don't copy code.

## Sources
- Aikhjarto/LUMIX-G9II-Remote-Control, `LumixG9IIBluetoothControl.py`, `parse_bluetooth_log.py`, README "Logging Bluetooth commands": https://github.com/Aikhjarto/LUMIX-G9II-Remote-Control
- MRSa/A01d (Olympus BLE only): https://github.com/MRSa/A01d
- Panasonic S5 Operating Instructions via manualslib: Remote Wakeup p.440, Pairing p.420-421, Connecting p.423, Location Logging p.439, Auto Clock Set p.441, Wi-Fi Setup menu p.468: https://www.manualslib.com/manual/1910359/Panasonic-Lumix-S5.html
- LUMIX Sync DC-S5 support page: https://av.jpn.support.panasonic.com/support/global/cs/soft/lumix_sync/en/DC-S5.html?modelnm04=S5
