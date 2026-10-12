# Lumix DC-S5 (fw 2.03) Bluetooth LE research

Compiled 2026-10-11. Tags: **[S5]** = observed on this camera, **[G9II]** = from Aikhjarto/LUMIX-G9II-Remote-Control (GPL-3.0, facts only, no code copied), **[manual]** = Panasonic S5 Operating Instructions, **[?]** = inference.

Raw artifacts: `probe-results/ble/` (scan1.json, gatt1.json, read1.json, read2.txt, idle1.txt, scan2.txt and the scripts that produced them). Venv: `.venv-ble` (Homebrew Python 3.14 + bleak 3.0.2; the system Python 3.9 can't build pyobjc).

No GATT writes, no notify subscriptions (those are CCCD writes), no pairing, nothing on the Wi-Fi API in this run.

## 1. What was confirmed on this S5

- **macOS BLE access works.** bleak (CoreBluetooth) scanned straight away from the terminal on macOS 26.6, with no permission prompt or error, so the terminal already has Bluetooth permission. A backend started some other way (launchd, a .app bundle) needs its own Bluetooth TCC grant (`NSBluetoothAlwaysUsageDescription`).
- **Advertisement [S5]** (camera on, Wi-Fi remote session active, Bluetooth on):
  - Local name `S5-XXXXXX` (same as the Wi-Fi device name / ddd friendlyName).
  - Manufacturer data, company 0x003A (Panasonic): `00 7a 90 xx xx xx`: the state byte (see 1b), then the last 5 bytes of the camera's public BT address (Panasonic OUI; address redacted here).
  - Service UUID advertised: `054ac620-3214-11e6-ac0d-0002a5d5c51b`.
  - Connectable, RSSI about -40 dBm at desk distance. The macOS address is a per-host CoreBluetooth UUID (`00000000-0000-0000-0000-000000000000`), not the MAC. Match on name or manufacturer data, not on address.
- **Unauthenticated connect works, GATT discovery works**, MTU 185. With no traffic, an idle connection stayed up for 40 s and we closed it ourselves.
- **Any characteristic read drops the link** (see 1b: application-level rejection, not encryption). Reading `054ac621-...-0001-...` (an info-looking char) and separately the suspected challenge char `03e23a31-...` both ended in `BleakError('disconnected')` within a few seconds. No bond was created (`system_profiler SPBluetoothDataType` lists nothing). Interpretation [?]: the characteristics need an encrypted, bonded link. CoreBluetooth reacts to "insufficient authentication/encryption" by starting pairing, and the camera, which is not in pairing standby and doesn't know the Mac, drops the connection. **Conclusion: the Mac must be bonded via the camera's Pairing menu before any GATT traffic.**

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

## 1b. Pairing attempts from macOS, 2026-10-11 evening [S5]

- **Advertisement state byte** (first byte of the 0x003A manufacturer data) is the camera's BLE state. LUMIX Sync names the values: 0 normal, 1 sleep, 2 wakeup, 3 pairing, 4 sleep_pow_off, 5 sleep_pow_on, 6 sleep_pow_off_fast, 7 sleep_pow_on_fast, 8 sleep_pow_on_autotrans, 9 sleep_pow_off_fast_autotrans, 10 sleep_pow_on_fast_autotrans [APK]. Seen on this S5: `00` (on, normal) and `03` (Bluetooth > SET > Pairing screen). The other five bytes are the camera's public BT address.
- **The camera uses resolvable private addresses.** Each Bluetooth on/off or Pairing entry gave a new random address and a new CoreBluetooth UUID. The public address only shows up in the advertisement payload.
- **Why reads dropped the link (corrects section 1).** bluetoothd logs (`/usr/bin/log show --predicate 'process == "bluetoothd"'`; in zsh `log` is a builtin, so call `/usr/bin/log`) show the camera ending the link with HCI reason 0x13 (remote user terminated) about 200 ms after the Mac's first *read* request. This happened in both normal and Pairing mode. There's no ATT error, no SMP security request, and macOS never tries to pair (`encrypted:0`, `isPairing=0`). So this isn't a failed encryption attempt. The camera rejects a read from an unregistered central at the application level.
- **What the camera did NOT drop** (unbonded, Pairing mode): idle connections (40 s+), CCCD writes (notify on e182ec43, 37701b80, 4cf487c0), and a write-without-response to 7be5fd56. Reads of a09fad60 and 20f5fffc timed out after 20 s instead of dropping. e182ec41 has no CCCD before registration ("attribute not found").
- Remote version info: LMP 7 (Bluetooth 4.1), manufacturer 70 (MediaTek), LE features `01` (encryption only). That means LE Legacy pairing only.
- **macOS has no public API to start LE bonding.** bleak's `pair()` raises NotImplementedError. CoreBluetooth's private `CBPairingAgent pairPeer:` is gated: bluetoothd logs "programmatic pairing disabled" for non-entitled apps. `blueutil --pair <public addr>` *does* make a BR/EDR (classic) bond: the chip answers as "BLUETOOTH_AUDIO", vendor 0x0046. That bond doesn't carry over to LE (no CTKD on a 4.1 chip), and it made the camera leave Pairing mode, so it was removed again (`blueutil --unpair`). Don't use it.
- S5-XXXXXX never appears in System Settings > Bluetooth (macOS only lists LE devices it has a reason to show, e.g. HID).

## 1c. LUMIX Sync pairing flow (from Android APK 2.0.17) [APK]

Source: `com.panasonic.jp.lumixsync` 2.0.17 (versionCode 20017009, Aptoide mirror, md5 6e6212ac3ed8a093e3ff2b7fe09b8323), decompiled with jadx 1.5.6 for reading only. These are facts, not copied code. The BLE client lives in obfuscated classes `I5.e` (GATT queue), `I5.g` (manager + callbacks), `K5.j` (connect request), `z5.b` / `z5.d` / `N5.o` (encoders).

**The app never calls `createBond()`.** Bonding is left to the OS, which bonds when the camera demands encryption. The registration is application-level and comes before any read.

Connect sequence for bodies *without* the `bb5d2a8c-5d3b-43ad-bfc3-470cad55c1ca` ("UUID_APS") characteristic, which includes the S5:
1. `connectGatt`, then discover services.
2. Enable notifications (CCCD 0x2902 = 01 00) on these, in this order: `4cf487c0` (clock request), `37701b80` (GPS), `18345be1` (Wi-Fi connect-type ack), `48669da0`, `e182ec43`, `da534d0a`, `d611b4a0`, `e182ec41`, `e182ec42`, `7be5fe6e`. Then probe for `a31a7a87…` and service `a2e506df…`; both are absent on the S5.
3. Write `8d08a420` (svc 054ac620) = **DLNA_UUID**: 16 raw bytes, the hex string `4D454930010010008001` + a 12-hex-digit per-install seed. This is the same id format the Wi-Fi `accctrl req_acc` uses (`4D454930-0100-1000-8001-xxxxxxxxxxxx`), so the BLE registration and the Wi-Fi client are the same identity.
4. On that write's response, write `cd7a71a0` = **FRIENDLY_NAME**: ASCII device name (the phone model, or a user-set BT name), raw.
5. Read `e206a5c0` = **CAMERA_SSID** (null-terminated string), then `03e23a31` = **INITIAL_INFORM**: 4 bytes, big-endian, used as an XOR key (not a challenge to answer on S5-gen bodies). Then `c97cf1a5` = **CAMERA_WIFI_PASSWORD**, the camera's own AP password XOR-obfuscated with that key (key defaults to 0x902E2A2C, i.e. -1875740628, when 0).
6. When the camera notifies `4cf487c0`, the app writes the clock to `ead55e60` (10-byte payload, see section 2).

Bodies *with* `bb5d2a8c` (G9II and newer, "APS") do it differently: read `03e23a31` first, then write `bb5d2a8c` with the UUID XOR-encoded against INITIAL_INFORM (20 bytes), then a name to type 17 (`7b61774f`). That 20-byte XOR is what Aikhjarto captured on the G9II. **It doesn't apply to the S5.**

Wi-Fi commands (write types in the app):
- `0d6f1880` (svc 5ae57260) = WIFI_CONNECT_TYPE: `01` camera SoftAP, `02` STA (join a network), `03` "SSID written, use it".
- `18345be0` = SSID, ASCII zero-padded to 32 bytes (the password is never sent; the camera has to know the network already).
- `e182ec40` (svc 054ac620) = Wi-Fi start / purpose byte. Values 1-6 are used. The app sends 3 or 5 right after the connect-type write; the G9II capture showed 3 after an STA join and 5 for AP remote shooting.
- STA flow: write SSID → camera acks → write `0d6f1880=03` → write `e182ec40=<purpose>`.
- `7be5fd56` / notify `7be5fe6e`: a small request/ack channel (the app writes 02 when the notify answers 00 to a 01 request).
- `daff1bc0` = GPS (Base64-decoded blob from the app), `ead55e60` = clock.

Read types: `e206a5c0` SSID, `c97cf1a5` AP password, `03e23a31` INITIAL_INFORM, `e1392720` (svc 6b005f20), `3d22c6be` (svc 7ddc40a0, XOR-decoded "DAC" string), `054ac621/622/623` info.

## 1d. Confirmed on this S5 (2026-10-11 night) [S5]

**Registration works from macOS without any bonding.** No SMP, no encryption, no macOS dialog. Steps:
1. Connect.
2. Optionally try the notify subscriptions. macOS answers "attribute not found" for all ten: CoreBluetooth discovers no CCCDs on the S5. The camera sends notifications anyway (18345be1 and 7be5fe6e notifies were received), so this is harmless.
3. Write `8d08a420` = 16-byte MEI0 client id (`4D454930 0100 1000 8001` + 6 bytes; we use the same id as our Wi-Fi `req_acc`).
4. Write `cd7a71a0` = ASCII name ("LumixRemote").
5. Reads work after that.

A *read* before step 3 is what killed every earlier attempt (HCI 0x13). bleak/CoreBluetooth issues no implicit reads of its own; only MTU exchange and discovery run before our first request.

**First-time registration (Pairing mode) also needs the Wi-Fi leg.** After the BLE writes, the camera shows "SSID: S5-XXXXXX Connecting to Wi-Fi" and starts its SoftAP. It shows "Pairing failed" (and switched Bluetooth off) when no client joined. What worked, reproducing LUMIX Sync:
1. BLE: read `e206a5c0` (AP SSID = "S5-XXXXXX"), `03e23a31` (key) and `c97cf1a5` (AP password: empty on this body, i.e. an open AP).
2. The Mac joins the AP (`networksetup -setairportnetwork en0 S5-XXXXXX`) and gets 192.168.54.10.
3. `accctrl req_acc` with the same MEI0 id at 192.168.54.1: `ok_under_research_no_msg` → 1.5 s later `ok,S5-XXXXXX,remote,open`. The camera then says "Pairing completed".
4. Forget the AP and toggle Wi-Fi; the Mac was back on the home network about 5 s later.

`bin/lumix-ble register` / `POST /api/ble/register` do exactly this. The Mac is off the home LAN for about 30 s. Without a second network interface that can't be avoided.

**Reads after registration** (camera on, normal state):

| UUID | value |
|---|---|
| `e206a5c0` | camera AP SSID, 32 B NUL-padded ("S5-XXXXXX") |
| `03e23a31` | INITIAL_INFORM, 4 random bytes per connection (XOR key) |
| `9c781c60` | 1 byte (`01`) + the camera's saved home SSID, NUL-terminated. Used as the default network for "join" |
| `054ac621` | `02000000` |
| `054ac622` | "Panasonic" |
| `054ac623` | "DC-S5" |
| `69e4d998`, `a09fad60`, `20f5fffc` | never answer (time out), and the link drops ~30 s later. Don't read them. |
| `c97cf1a5` | AP password, not read in normal use |
| `e1392720`, `3d22c6be` | not measured (camera was busy in that run) |

**Wake from Wi-Fi-off / OFF, the open problem.** On the first try (21:59, camera on with Wi-Fi off, Remote Wakeup still OFF), the join sequence below brought Wi-Fi up 40 s after the commands, and the camera dropped BLE at the same moment. Later tries (after Remote Wakeup was switched ON and the camera was power-cycled by hand) behaved differently:
- The camera accepted every write and acked `18345be1 = 00`.
- The advertisement state went to `02 wakeup`.
- Wi-Fi never appeared on the LAN (no ARP entry, no :60606 anywhere) within 60-120 s, whether or not BLE stayed connected.
So the camera does *something* on wake but waits for a step we don't send yet. Prime suspect: with Remote Wakeup the app may expect to join the camera's SoftAP (LUMIX Sync's "wakeup" handling in HomeActivity). Next test: after a wake, look for the camera's SoftAP (`S5-…`) with a Wi-Fi scan, and read `e1392720` (camera IP, per the Image App prior art). Measuring power-off → wake timings is blocked on this.

**Wake from switch-OFF, 2026-10-12 (trace: tests/fixtures/real-s5-ble-wake-2026-10-12.log).**
- Connecting to the sleeping body takes 24-27 s (bleak's 20 s timeout produced the UI's bare "TimeoutError:"). Now 45 s, one retry.
- The S5 hangs up ~0.3 s after the friendly-name write when the MEI0 id isn't the one it was paired with. Our id had changed overnight: `uuid.getnode()` picks a different interface in the server's Python 3.9 and the BLE helper's 3.14 (and changes with USB adapters). The default id is now derived from the machine id; the camera entry's `client_uuid` can be PATCHed back to the paired id.
- With the paired id, SSID, `03`, start `01` powered the body up (adv state 02 -> 00, the screen came on) but it never joined Wi-Fi.
- LUMIX Sync's Remote-shooting sequence (APK 2.0.17, I5.g write-complete chain): SSID -> `0d6f1880=03` -> ack `18345be1=00` -> `0d6f1880=02` (STA) -> `e182ec40=03`. That brought the camera onto the home network 11.7 s after the commands.
- After a BLE wake with the switch OFF the body waits in a standby (play, lens unpowered) until the first recmode, and goes back to sleep when no cam.cgi request reaches it for ~30 s in that state (a fully-on body kept a silent session for 75 s; 2026-10-12 14:30: our connect waited 30 s for a mod_Play that never came, sending nothing; the getstate after the wait got no reply). LUMIX Sync's 1 Hz getstate starts right after `device_name`; the connect now polls getstate during that wait too (tests/fixtures/real-s5-ble-wake-settle-2026-10-12.log).
- After that, the server's Wi-Fi handshake didn't finish: req_acc under the old id got `err_user_refused`; under the paired id cam.cgi replies came back one request late (e.g. a `getinfo lens` reply to `req_acc`), so the camera looked like it rejected the session. Then the battery ran out. Every wake powers the body up, so wakes are now capped at 2 per 10 min.

**Wi-Fi join over BLE (verified 6+ times while the camera's Wi-Fi was already up, once from Wi-Fi-off).** After registration:
1. Write `18345be0` = home SSID (32 B NUL-padded).
2. Write `0d6f1880` = `03`; the camera notifies `18345be1 = 00` about 0.15 s later.
3. Write `e182ec40` = `01`.
With the camera's Wi-Fi off, it answered `:60606/ddd` at its usual DHCP address about 40 s after the commands. With Wi-Fi already up, `ddd` answers about 2 s after the commands.

Clock write `ead55e60` (10 B) is accepted.

Typical BLE timings: scan+connect 3-16 s (most of it is CoreBluetooth connection setup, ~7 s), registration writes +0.4 s, whole Wi-Fi command set by +10-18 s.

**SoftAP over BLE.** Writing `0d6f1880 = 01` then `e182ec40 = 05` made the camera leave the home network (`ddd` gone after ~40 s). A later STA join brought it back (`ddd` up 19.5 s after the wake started).

**BT remote controller (`7be5fd56`).** Writes are 1 = confirm (notify `7be5fe6e = 04`), 2 = start (notify `00`), 3 = finish. Entering it *switches the camera's Wi-Fi off*, and afterwards STA joins (purpose 1/3/5) left the camera stuck on a "Connecting" screen until a manual power cycle. Don't use it on a camera you want on Wi-Fi.

**The err_busy wedge vs BLE (the reason this work started).**
- While wedged, every `camcmd` answers `err_busy` (recmode, playmode, menu_exit, capture_cancel), and `camcmd poweroff` answers `err_reject`. It also answers `err_reject` in play mode, even when not wedged, so switch to rec first.
- A full BLE-driven Wi-Fi restart (SoftAP and back to STA, new req_acc) does **not** clear the wedge.
- LUMIX Sync 2.0.17 has no BLE power-off command; its only power-off is the Wi-Fi `camcmd poweroff`.
- **So a wedged S5 can't be power-cycled by software.** BLE wake is still useful for bringing Wi-Fi back after a successful poweroff, after the camera's own auto power-off (Remote Wakeup), or after Wi-Fi was turned off. Not yet measured: waking from OFF/sleep with Remote Wakeup on (adv states 4-7).

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
