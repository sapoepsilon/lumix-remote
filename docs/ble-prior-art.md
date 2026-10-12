# LUMIX BLE pairing: prior art survey

Compiled 2026-10-11 from read-only web/GitHub research. No camera access.
Legend: [CONFIRMED] = stated in primary source from real captures/decompiled app code, or hardware-verified by the author.
[INFERRED] = my reading of code/captures. [SPECULATION] = untested hypothesis.

## TL;DR for the "disconnect on first GATT read" problem

1. Nobody documents Panasonic using BLE bonding/SMP. Decompiled Panasonic Image App has no createBond/bond calls
   [CONFIRMED, grep]; tobiasbrummer's Android HCI snoop of LUMIX Sync on an S5 shows no SMP / Encryption Change /
   LTK Request across several fresh re-registrations [CONFIRMED]. "Pairing" is application layer.
2. The first GATT operations the vendor apps perform are NOT reads:
   - Image App (Android, decompiled): `discoverServices` -> write CCCD 0x0001 on ~11 notify characteristics -> WRITE `MEI0`
     identity to 8d08a420 -> WRITE phone name to cd7a71a0. Reads only come after those writes [CONFIRMED from code].
   - LUMIX Sync on S5 (tobiasbrummer HCI snoop): same shape: subscribe -> MEI0 write -> name write -> wait for ready
     notifies -> time write -> GPS writes [CONFIRMED].
3. The S5 enforces the order by dropping the link: skipping steps (MEI0 straight to GPS) made it disconnect [CONFIRMED,
   tobiasbrummer]. A camera-initiated disconnect shows as HCI 0x13 (he sees 0x13 on normal camera power-off too).
   So your 0x13 on an early read is most likely the camera's app-layer state machine, not a security rejection
   [INFERRED; SPECULATION that "read before MEI0" is the exact trigger, nobody tested it].
4. Working unbonded central on S5: ESP32 NimBLE, connect with no pairing, subscribe both ready-notifies, write MEI0,
   write name, wait for both notifies (=0x01), write time, write GPS. Hardware-verified [CONFIRMED, tobiasbrummer].
   Caveat: he replayed MEI0 using the identity of a phone already registered via the app. Whether a brand-new MEI0
   identity is accepted outside Pairing mode is untested [SPECULATION]; Pairing mode should accept a new one.
5. Likely Mac-side trap [SPECULATION]: bleak/CoreBluetooth service discovery may issue ATT reads (descriptors, or the
   Device Name / GAP chars) before your code runs. Check with a PacketLogger/btsnoop capture of what bleak sends
   first. Workaround to try: use raw CoreBluetooth (PyObjC) or an ESP32/Linux central, discover services, then
   immediately `writeValue` CCCDs, then MEI0 with response, with no reads at all.

## Identity token (MEI0) - what it really is

Image App source: write to 054ac620-.../8d08a420-... of the 16 bytes
`4D454930 0100 1000 8001` + 6-byte `Dlna_UUID_Seed` (app-instance random value, stored in prefs, generated once by the
DLNA lib). The same string formatted as UUID `4D454930-0100-1000-8001-<seed>` is sent to the camera over Wi-Fi
HTTP as the client id during Wi-Fi registration [CONFIRMED in decompiled code]. So MEI0 = "ME I0" UUID prefix + a
persistent per-install ID, i.e. the camera's registered-device identity.
tobiasbrummer called the last 4 bytes "counter + CRC16"; his two captures differ only because he wiped app data
between them (new seed). He found a fixed value accepted over many reconnects, consistent with it being a stable ID,
not a counter/CRC [INFERRED from the two sources agreeing].

## Sources

### 1. tobiasbrummer/lux-lat-long-log  (BEST: LUMIX S5, same UUID, hardware-verified)
- URL: https://github.com/tobiasbrummer/lux-lat-long-log (PROTOCOL.md, firmware/src/panasonic_s5.cpp, probe.py, parse_hci.py)
- License: MIT. Pushed 2026-07-04. Language of docs: PROTOCOL.md German, README English.
- Models: DC-S5 only (advertised name `S5-XXXXXX`, e.g. S5-30E2BA). Capture: Pixel 8 Pro, LUMIX Sync, Android HCI snoop.
- Roles: camera peripheral with RPA (rotating address ~15 min), match by name prefix `S5-` or by service UUID.
- Security: no SMP, no bonding, ATT writes in clear [CONFIRMED]. Camera power-off = camera-sent disconnect 0x13.
- GATT (all suffix -0002a5d5c51b; resolve by UUID not handle):
  - Session svc 054ac620-3214-11e6-ac0d: 8d08a420-3213-11e6-8aca (W, MEI0 16B), cd7a71a0-3213-11e6-8f56 (W, phone name ASCII)
  - Location svc 1d74afe0-3214-11e6-8ab4: daff1bc0-3216-11e6-91c8 (W, 16B GPS), 37701b80-32a7-11e6-bb83 (N, 0x01 ready)
  - Clock svc 34738720-3214-11e6-b66b: ead55e60-3216-11e6-a42e (W, 10B time), 4cf487c0-32a7-11e6-a50d (N, 0x01 ready)
  - Seen but undecoded: svc 7be5faae-475b-11e7-... char 7be5fd56 writes 0x01 every 3-4 s (heartbeat? see source 2 for
    real meaning = remote shutter); svc 5ae57260 char 0d6f1880 one-shot 0x03.
- Verified order: enable notify on 37701b80 + 4cf487c0 -> write MEI0 -> write name -> wait both ready notifies ->
  write time -> write GPS (~1 Hz; one write is enough to geotag, camera caches last fix).
- Sample MEI0 (hex): 4d454930 0100 1000 8001 0200 0003 0b54 (his probe.py MEI0_KNOWN_GOOD uses ...0000030b54 form).
- GPS packet 16B LE: u32 GPS-epoch seconds (unix-315964800), i32 lat*1e7, i32 lon*1e7, u16 alt m, u8 0x41 fix, u8 0.
- Time packet 10B: u16 year LE, month, day, hour(UTC), min, sec, then `3c 00 01` = tz offset minutes as s16 (0x3c=60) + DST flag
  (decoded via source 2's Java; he left it open).
- probe.py (bleak): scan, connect, list GATT, subscribe, `--write` does MEI0+GPS. Note: it does NOT test an unbonded
  macOS central first-op behavior; no log of results included. Untested on macOS.
- Doesn't describe Pairing mode, wake, or Wi-Fi handoff.

### 2. maiermic/panasonic-image-app  (decompiled Panasonic Image App 1.10.14, Android; the authoritative GATT map)
- URL: https://github.com/maiermic/panasonic-image-app  key file:
  panasonic-image-app_1.10.14/source/com/panasonic/avc/cng/model/service/imageapp/ImageAppTotalService.java
  (also view/bluetooth/BluetoothRemoteControllerActivity.java, view/bluetooth/BluetoothRegistActivity.java,
  view/cameraconnect/GuidanceMenuActivity.java)
- License: none declared (GitHub shows no license). It is decompiled proprietary Panasonic code: use as a reference for
  facts (UUIDs, order), do NOT copy code. Panasonic's predecessor to LUMIX Sync; cameras: GH5-era "Image App"
  Bluetooth cameras (GH5/G9/GX9/G100-class) - exact model list not in source [UNKNOWN which]. Same UUIDs as S5 =>
  LUMIX Sync inherited the protocol [INFERRED].
- Connect flow [CONFIRMED]: `connectGatt(autoConnect=false)`; 20 s timer then disconnect; `onConnectionStateChange(2)` ->
  `discoverServices()`. No createBond, no MTU request, no connection-priority call.
- onServicesDiscovered: enables notifications (setCharacteristicNotification + write CCCD 2902 = 0100) on, in order:
  4cf487c0-32a7... (svc 34738720), 37701b80-32a7... (svc 1d74afe0), 18345be1-3217... (svc 5ae57260),
  48669da0-3217... (svc 6b005f20), e182ec43-3213..., e182ec41-3213..., e182ec42-3213... (all svc 054ac620),
  da534d0a-63a3-447c-a889-aab701906af2 and d611b4a0-3217-11e6-8d98 (svc f3b05360-3215-11e6-8529),
  7be5fe6e-475b-11e7-a919-92ebcb67fe33 (svc 7be5faae-475b-11e7-a919-92ebcb67fe33). Missing chars are skipped.
  Only after that: write MEI0 (only when "registering" or in an app-initiated connect state).
- Write-complete chaining: MEI0 write ok -> write device name (cd7a71a0; falls back to "SmartPhone") -> name write ok:
  if registering, READ 054ac620/03e23a31-a54c-40fa-a668-de9acdc910bb (4B key, BE, used to obfuscate Wi-Fi password on
  newer cameras) -> READ e206a5c0-3214-11e6-afe4 (Wi-Fi SSID) -> READ a09fad60-3215-11e6-bdc0 or
  c97cf1a5-3c03-4290-8c1b-9e74b9500f54 (Wi-Fi password) -> write 5ae57260/0d6f1880-3217-11e6-a4cb = 0x01 ->
  write 054ac620/e182ec40-3213-11e6-ab07 = 0x05 (registration done). So the first READ in the vendor flow is
  gated behind MEI0 + name writes [CONFIRMED from code structure].
- Advertising [CONFIRMED]: parses AD type 0xFF; first byte 0x3A (company 0x003A) then byte[2] = camera state:
  0 normal, 1 sleep, 2 wakeup, 3 pairing, 4 sleep_pow_off, 5 sleep_pow_on, 6 sleep_pow_off_fast, 7 sleep_pow_on_fast;
  following 6 bytes = camera address/id compared against the stored paired id. Name from AD type 0x09.
  Use this to detect Pairing mode (state 3) and sleeping camera (1/4/6) without connecting.
- Notifications: 4cf487c0 any -> app writes time packet; 37701b80 value 1 = start GPS stream, 2 = stop, 3 = send cached
  fix; d611b4a0 / da534d0a = backup/transfer progress (da534d0a: u32 LE left, u32 LE total).
- Time packet (Java) [CONFIRMED]: LE u16 year, u8 mon(1-12), day, hour(UTC), min, sec, s16 tz minutes, u8 dst flag.
- GPS packet [CONFIRMED]: LE i32 time field, i32 lat*1e7, i32 lon*1e7, i16 alt, u8 status, u8 0. (2147483647 = no fix.)
- Remote shutter/record [CONFIRMED]: service 7be5faae-475b-11e7-a919-92ebcb67fe33, write char 7be5fd56-475b-11e7-a919-
  92ebcb67fe33, notify 7be5fe6e-...: values 1=enter remote (after MEI0+name), 2=start/ready, 3=(3 used as state),
  4=shutter press, 5=shutter release, 6=record start, 7=record stop; notify 0 => ready.
- "Wi-Fi via Bluetooth" [INFERRED]: svc 5ae57260-3214-11e6-979e: 0d6f1880 write 1=SoftAP/start AP, 2/3 = other
  states; 18345be0-3217-11e6-b56c write 32-byte NUL-padded SSID for STA mode (join phone's network); notify 18345be1
  (0 vs nonzero status). Camera IP read: svc 6b005f20-3214-11e6-a5ac char e1392720-3215-11e6-a035. Selector char
  054ac620/e182ec40 written with a mode 1..5 (mode meaning not fully traced). Newer cameras use a second UUID family
  (3d22c6be-baa6-451b-aa2c-f96c50005910, 36d3228b-188c-474f-a262-e0f01c5a367d, 1b7ff948-8e4d-43d7-8659-737e9540e231,
  7beca6bb-58fc-4b27-80c0-6ffc7f6ba4c8, 9e5288ca-f50e-43cf-9213-1c277571f29c, c97cf1a5-...) for obfuscated SSID/pass.
- Feature probe service 054ac620-3214-11e6-0001-0002a5d5c51b (chars 054ac621/622/623-...-0001-...) = camera-setting
  support/exposure; presence checked before showing UI.
- Remote wakeup: no dedicated GATT write found. App passively scans (startLeScan), reads adv state; if state is
  sleep/wakeup/sleep_pow_* and the id matches a registered camera it calls connectGatt, i.e. the BLE connection
  itself (with MEI0 identity) is what wakes the camera [INFERRED; not verified on hardware].

### 3. Aikhjarto/LUMIX-G9II-Remote-Control  (newer protocol generation, DC-G9II; bleak)
- URL: https://github.com/Aikhjarto/LUMIX-G9II-Remote-Control (src/LumixG9IIRemoteControl/LumixG9IIBluetoothControl.py,
  parse_bluetooth_log.py, README.md). License: GPL-3.0 (do not copy into a non-GPL project; reimplement from facts).
- Models: DC-G9M2 (name regex ^G9M2.*); covers both "LUMIX Sync" and "LUMIX Lab" app logins. Pushed 2024-12-31.
- Method: Android HCI snoop (README), Wireshark btatt. Uses bleak on Linux, handle-based (characteristics[handle-1]);
  no UUIDs for the main chars recorded. Two notify UUIDs mentioned: dcba3a74-80bc-4919-8ef4-0c9f99cc20dd,
  16726c35-52ef-4d00-868d-099549a90d9b (G9II-gen UUID family, differs from S5's -0002a5d5c51b family).
- LUMIX Sync login on G9II [CONFIRMED by author's working code]: start_notify on notify chars (skipping 0x39,0x3F,0x45,0x69),
  READ handle 0x2A (4 bytes, challenge) -> compute two 20-byte responses: each of 5 BE u32 words = challenge XOR
  {49454d10,10000130,02018000,450200a0,ffffff18} -> WRITE 0x2C ; {35504603,0,0,0,ffffff00} -> WRITE 0x2E ; then clock
  write 0x44 (u16 year,mon,day,hour,min,sec,0,s16 tz) ; read name 0x36. Lumix Lab uses 0x7A challenge ->0x72/0x74
  (+ "LUMIX LUT Creators APP 1.2.1" name at 0x70, writes without response).
  Note `49454d10`/`4d454930` family: the "MEI" magic persists in the new challenge scheme [INFERRED].
- No pairing/bonding code; README mentions `bluetoothctl -m` for debugging only. Whether the host had been bonded or
  Lumix-registered before is not stated [UNKNOWN]. Relevant to you: on this generation the first op IS a read (challenge),
  so a read-first policy is not universally rejected; S5/older gen differs [INFERRED].
- Other handles (G9II): 0x68 shutter/record (1,2,4 press; 5 release; 6,7 record), 0x3E GPS 16B
  (header 0x5486AF20+n, lat/lon*1e7, constant), 0x8E GPS enable (1/2, Lab), 0x4A/0x30/0x4E Wi-Fi AP + STA SSID(32B),
  0x7C Lab AP enable, 0xA0 5 GHz. Notes in code comments, partially speculative per author.

### 4. Dead ends / adjacent (checked, not useful for BLE)
- njfdev/liblumix, Xemixo/lumix-monitor, Mcsiv/nodeLumixControl, axelw3/lumix-remote, Tontunstuff/Lumix-Remote: Wi-Fi/HTTP
  or Panasonic SDK only. slimdroid/lumix-esp32: unrelated (LED garland). gkoh/furble (ESP32 multi-brand remote):
  Fujifilm/Canon/Ricoh/Nikon/Sony, no Panasonic found. No Home Assistant/ESPHome/Flipper Lumix BLE integration found.
- Web (dpreview/eevblog/reddit/ReverseEngineering/blogs/theses): searches surfaced nothing Lumix-BLE specific. Coverage is
  limited to the search engine used; forum threads may exist but were not found [UNKNOWN]. GitHub code search for
  "054ac620" returned only sources 1 and 2.

### 5. Official user-facing docs (Panasonic)
- Pairing: https://av.jpn.support.panasonic.com/support/global/cs/soft/lumix_sync/en/DC-S5M2/connect_bt.html
  Camera: Setup > IN/OUT > Bluetooth > Pairing (shows device name); app selects name; no passkey; iOS shows a "Join" prompt;
  max 5 registered cameras, one connected at a time.
- Remote Wakeup: https://av.jpn.support.panasonic.com/support/global/cs/soft/lumix_sync/en/DC-S5M2/adv_rem_wu.html
  Requires prior BT registration, Bluetooth > Remote Wakeup ON, power switch OFF; opening Remote shooting/Import
  turns camera on and joins Wi-Fi; BT stays powered (battery drain).
  (Documented for S5II; S5 menu likely similar [SPECULATION].)

## Suggested next experiments (cheapest first)
1. Re-run your Mac connection but log the actual first ATT PDU (PacketLogger / `sudo log stream` / ESP32 sniffer) to see whether
   bleak/CoreBluetooth sends a read before you do.
2. From any central (ESP32 NimBLE is proven): connect in Pairing mode, enable CCCDs in the Image App order, write MEI0 with a
   fresh `4D454930 0100 1000 8001 + 6 random bytes`, write name, wait for notifies, then read 03e23a31/e206a5c0.
3. Parse mfr data byte[2] (0x3A,0x00,state,...) to confirm state 3 = pairing and to learn the sleep/wakeup states for wake.
4. For wake: with Remote Wakeup on and the switch OFF, scan the adv state, then connect + MEI0 as a registered identity; log
   what it takes (sleep states 1/4/6 are the candidates).
