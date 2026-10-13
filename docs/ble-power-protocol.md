# Lumix S5 power and Wi-Fi over Bluetooth LE: what LUMIX Sync does, and why our wake fails

Compiled 2026-10-13. Read-only study: nothing was sent to the camera for this document.

Sources:
- LUMIX Sync for Android 2.0.17 (`com.panasonic.jp.lumixsync`, versionCode 20017009, sha256 `6986e22f…e377`, the same build as `docs/lumix-sync-wifi-flow.md`), decompiled with jadx 1.5.x and never installed or run. Class names are jadx/obfuscator names, cited as `file:line` in the jadx `sources/` tree. These are facts about the protocol, not copied code.
- Panasonic Image App 1.10.14 (older app, readable names) for cross-checks.
- Our BLE traces from 2026-10-11 to 2026-10-13: `tests/fixtures/real-s5-ble-wake*.log`, `probe-results/ble/advwatch*.txt`, the server log, and agent session logs. All are scrubbed here: `<id>` is our MEI0 client id, `<ssid>` is the home SSID payload, `<addr>` is the camera's BT address bytes.
- Panasonic's LUMIX Sync help pages (Remote Wakeup, Returning from Sleep Mode); links at the end.

Tags: **[APK]** = read from LUMIX Sync code. **[S5]** = seen on our DC-S5 (fw 2.03). **[doc]** = Panasonic help text. **[?]** = inference.

---

## 0. Summary

- **Our BLE writes are byte-for-byte the same in the successful and the failed wakes.** Only the per-session values differ: the clock and the INITIAL_INFORM key. What decides the outcome is the camera's state, which our code never looks at (section 4).
- **LUMIX Sync doesn't use one fixed sequence.** It picks the Wi-Fi "purpose" from the advertised state byte [APK]:
  - In the `_fast` sleep states (6/7/9/10) it always asks for the camera's own access point: purpose `05`, connect type `01`. The phone then joins the camera's SSID.
  - Only in the other states does it use purpose `03`, with STA when the phone is itself on Wi-Fi.
  - Our failing 2026-10-12 23:19 wake started from state 6 `sleep_pow_off_fast`, and we asked for STA anyway.
- **When the camera is asleep, LUMIX Sync is built around two BLE connections, not one.** It connects once to wake the body. Then it waits for the camera to advertise `wakeup` (2) and connects again to run the Wi-Fi request (`HomeActivity.java:1230-1259`). Our only "split" success, runs B → C at 13:18/13:26 on 2026-10-12, followed exactly this shape.
- **The camera reports the result of the Wi-Fi start command on `e182ec41`.** `01` means "Due to the state of the camera, this function cannot be used" [APK]. We have never received `e182ec41`, `e182ec43` or `4cf487c0`. On macOS every CCCD write fails with "attribute not found", so the camera's error and request channel is invisible to us.
- **There is no BLE power-off for the S5.** The app's only power-off on this generation is Wi-Fi `camcmd poweroff`. A BLE power command (`96c82c33`) exists only on newer bodies.

---

## 1. GATT map used by the app (S5 generation)

Write types (`I5/g.java:2023-2112`, the same table as `I5/e.java:365-442`):

| type | characteristic (service) | meaning |
|---|---|---|
| 1 | `8d08a420` (054ac620) | DLNA_UUID: 16-byte MEI0 client id `4D454930 0100 1000 8001 <6 bytes>` |
| 2 | `cd7a71a0` (054ac620) | FRIENDLY_NAME, ASCII (BT name, else phone model) |
| 4 | `e182ec40` (054ac620) | WIFI_START "purpose", 1 byte |
| 7 | `daff1bc0` (1d74afe0) | GPS blob |
| 9 | `ead55e60` (34738720) | clock, 10 bytes |
| 11 | `0d6f1880` (5ae57260) | WIFI_CONNECT_TYPE: `01` SoftAP, `02` STA, `03` "SSID written" |
| 12 | `18345be0` (5ae57260) | STA SSID, 32 bytes NUL-padded |
| 16/17 | `bb5d2a8c` / `7b61774f` | XOR-encoded id/name, "APS" bodies only (not S5) |
| 39 | `7be5fd56` (7be5faae) | BT remote controller: 1 confirm, 2 start, 3 finish |
| 42 | `96c82c33` (6b005f20) | auto-transfer control; `03` used as a power-off in auto-transfer mode, newer bodies only (`HomeAppSettingActivity.java:191`). **Not in the S5 GATT table.** |

Read types (`I5/g.java:1508-1592`):

| type | characteristic | contents |
|---|---|---|
| 5 | `e206a5c0` | camera AP SSID |
| 6 | `c97cf1a5` | camera AP password, XOR(INITIAL_INFORM) |
| 15 | `e1392720` | camera IP, read only in the transfer purposes |
| 35 | `03e23a31` | INITIAL_INFORM (XOR key) |
| 36/37/38 | `054ac621/622/623` | info |
| 34 | `3d22c6be` | "DAC" |
| 41 | `c37c7e8d` | — |

The app never reads `9c781c60`. We read it to learn the camera's saved home SSID.

Notifications enabled right after service discovery, in this order (`I5/g.java:1126-1140`, `I5/e.java:280-297`): `4cf487c0`, `37701b80`, `18345be1`, `48669da0`, `e182ec43`, `da534d0a`, `d611b4a0`, `e182ec41`, `e182ec42`, `7be5fe6e`. Then it probes `a31a7a87` and service `a2e506df`; both are absent on the S5. CCCD = `01 00` (`I5/g.java:1594-1668`).

Notification meanings (`I5/g.java:373-659`, `HomeActivity.java:1049-1140`):

| notify | value → app action |
|---|---|
| `4cf487c0` | camera asks for the time → write clock (type 9). **This is the only time the app writes the clock** (`g.java:389-391`). |
| `37701b80` | 1 = start GPS logging, 2 = stop, 3 = send the last fix |
| `18345be1` | ack of connect type `03`. `00` → the app writes `02` (STA). Non-zero → it writes `01` (SoftAP fallback) (`g.java:431-496`, purpose-3 runnable `g.java:333-366`) |
| `e182ec41` | result of `e182ec40`. In the transfer purposes 1/6: `00` → read camera IP `e1392720`. In remote purposes: `01` → dialog "Due to the state of the camera, this function cannot be used. Make sure the camera is not in operation…" (`ON_BT_CANNOT_REMOTE_WAKEUP`, `HomeActivity.java:1097-1121`), and the purpose is reset |
| `e182ec43` | camera-initiated request. `1`/`4` → the camera wants a Wi-Fi connection: the app sets purpose 1/6 and answers with its own SSID (STA) or connect type `01` (SoftAP) depending on the phone's network. `2` → reset purpose. `3` → read SSID (`g.java:600-658`) |
| `48669da0` | image-transfer start/stop; re-sends a failed clock write |
| `7be5fe6e` | BT remote controller acks (`00` after `01` → app sends `02`) |

---

## 2. Advertisement state byte (full enum)

Manufacturer data AD (type `0xFF`): `3A 00 <state> <addr…>`. bleak strips the company id, so our `mfr` hex is `<state><5 address bytes>`. Decoded by `p202z5/b.java:53-85`:

| byte | name | app treatment |
|---|---|---|
| 0 | `normal` | camera on; auto-connect if it is the last-connected camera (`g.java:1924-1970`) |
| 1 | `sleep` | asleep, switch ON (sleep group `Z0`, `P5/b.java:828`) |
| 2 | `wakeup` | camera is booting and wants the app. If a remote purpose is pending, the app **connects again** (`HomeActivity.java:1235-1259`). With purpose 5 it also starts "高速起動" (fast boot): the phone joins the camera's AP with a 180 s timeout (`:1252-1257`) |
| 3 | `pairing` | Pairing standby |
| 4 | `sleep_pow_off` | switch OFF, Remote Wakeup armed |
| 5 | `sleep_pow_on` | switch ON, asleep |
| 6 | `sleep_pow_off_fast` | switch OFF, "fast" (`X0`, `P5/b.java:819`) |
| 7 | `sleep_pow_on_fast` | switch ON asleep, "fast" (`Y0`, `P5/b.java:823`) |
| 8 | `sleep_pow_on_autotrans` | auto-transfer variant |
| 9 | `sleep_pow_off_fast_autotrans` | `X0` |
| 10 | `sleep_pow_on_fast_autotrans` | `Y0` |

- `pow_on` / `pow_off` is the power-switch position. The BT remote controller refuses `sleep_pow_off*` with "cannot use for SW OFF" (`HomeActivity.java` `onClickBTRemoteController`).
- `_fast` [?]: most likely the camera's Bluetooth > "Returning from Sleep Mode" = Import/Remote priority, which "shortens the time to wake for import / remote shooting" [doc]. The app's handling (always SoftAP, "fast boot") fits a camera that brings its own AP up straight away.

Seen on our S5 [S5]:
- 2026-10-11 22:23 / 22:39: `07` → `02` after our connect → `00`.
- 2026-10-12 13:15: `02`.
- 2026-10-12 22:18-23:22 and the morning of 2026-10-13: `06`. At 23:22 one scan returned two advertisers at once (`00` and `06`, two resolvable addresses), during the post-wake standby.

---

## 3. The app's BLE sequences

### Registration (every connection; `K5/j.java:138-213`, legacy path `I5/g.java:1126-1157, 834-835`)
1. `connectGatt` (20 s connect timeout per try, re-armed up to 10× by the 20 s timer `g.java:285-329, 1405-1414`), then discover services (20 s).
2. Enable the 10 notifications above.
3. Write `8d08a420` = MEI0 id. On its write response, write `cd7a71a0` = name. The app **never calls `createBond()`**.
4. `K5.j` path: read `03e23a31`, then `e206a5c0`, then `c97cf1a5`. The legacy path reads `e206a5c0` first, then by purpose (below).

### (a) Wake from switch-OFF / sleep ("Remote shooting" tapped while the state is in the sleep group)
- `onClickLiveviewButton` (`HomeActivity.java:4717-4760`): purpose = **`05` if the state is `_fast` (X0/Y0), else `03`** (`:4736`, also `:5289`, `:5484` for Import/Send).
- The Wi-Fi check callback then shows "Bt Wakeup (wait until the camera wakes)" (`K0`) and connects (`E0`) (`HomeActivity.java:1876-1893`).
- When the camera advertises `wakeup` and the purpose is still pending, the app connects again (`:1235-1250`). For purpose 5 it starts joining the camera AP in parallel (`:1252-1257`, `G0(ssid, pw, …, 180)`).
- A BLE error during the wake dialog → `ON_BT_WAKEUP_CONNECT_ERROR` (`:1356-1370`). An `e182ec41 = 01` → `ON_BT_CANNOT_REMOTE_WAKEUP`.

### (b)/(c) Wi-Fi on for a camera that is awake: purpose 3 (STA or SoftAP) vs purpose 5 (SoftAP)

Purpose 3 (`I5/g.java:777-824, 969-998` + `18345be1` handler `:471-480`):
1. read `03e23a31` → read `e206a5c0` → read `c97cf1a5`.
2. Phone on Wi-Fi with a real IP → write `18345be0` = **the phone's current SSID**. Otherwise → write `0d6f1880 = 01` (SoftAP) and the phone joins the camera AP.
3. On the SSID write response: write `0d6f1880 = 03` (`:836-839`).
4. Wait for notify `18345be1`: `00` → write `0d6f1880 = 02`, non-zero → `01`. Up to 3 tries, 500 ms apart.
5. On that write's response: write `e182ec40 = 03` (`:972-978`).
6. On the `e182ec40` response: done on BLE (`HomeActivity.java:1449-1468`). The app finds the camera on the LAN (SSDP/ddd) or, for SoftAP, joins it with `G0(..., 90)`.
7. `e182ec41 = 01` aborts with "cannot remote wakeup".

Purpose 5 (`g.java:936-967, 790-797`):
- read `03e23a31` → `e206a5c0` → `c97cf1a5` → write `0d6f1880 = 01` → write `e182ec40 = 05`.
- The phone joins the camera's AP (`HomeActivity.java:1077-1091, 1449-1458`).

Purposes 1/6 (auto-transfer, camera-initiated via `e182ec43`):
- `18345be1 = 00` → `0d6f1880 = 02`, then `e182ec40 = 01|06`.
- `e182ec41 = 00` → read the camera IP from `e1392720`.
- This is not a remote-shooting path. Our `e182ec40 = 01` runs were using the transfer purpose.

### (d) Power OFF
- No BLE power-off on the S5. The app's "Power OFF" (`HomeAppSettingActivity.java:191-194`) writes `96c82c33 = 03` only on bodies with the ImageTransferEx characteristic `a31a7a87`. Otherwise it asks for confirmation and uses Wi-Fi `cam.cgi?mode=camcmd&value=poweroff` (`view/setting/a.java:28`).
- Panasonic help for another body (BGH1): "If you turn off the camera by pressing the camera ON/OFF button on the camera, [Remote Wakeup] will not work." [doc]. For S5-class bodies the help says to set the switch to OFF [doc].

### (e) Keepalive and boot wait
- No periodic BLE keepalive write exists. The link is held open. The only periodic writes are GPS fixes (type 7) while `37701b80 = 1`. The clock is written only when `4cf487c0` asks.
- While the body boots, the app waits on Wi-Fi: LAN search, or the camera AP join with a 90 s (180 s fast-boot) timeout. Connection timeouts are counted: 15 timeouts in a sleep state → shown as disconnected (`HomeActivity.java:1195-1212`).
- After the Wi-Fi session starts, the app's 1 Hz `getstate` keeps the camera up (`docs/lumix-sync-wifi-flow.md` §5).

---

## 4. Our logs: success vs failure, byte by byte

All runs used the same id `8d08a420 = 4d454930010010008001<id>`, name `cd7a71a0 = 4c756d697852656d6f7465` ("LumixRemote") and SSID `18345be0 = <ssid>` (identical 32 bytes in every run).

| run | start state | connect | writes after registration | result |
|---|---|---|---|---|
| 2026-10-12 13:18 (B) | `02 wakeup`, switch OFF | 24 s | clock, SSID, `0d6f1880=03`, ack `00`, `e182ec40=01` | body powered up (02→00), **no Wi-Fi** |
| 2026-10-12 13:26 (C) | `00 normal` (body still in the post-wake standby from B) | 24 s | clock, SSID, `03`, ack `00`, `02`, `e182ec40=03` | **Wi-Fi up 11.7 s** |
| 2026-10-12 14:29 | after Wi-Fi `camcmd poweroff` (switch ON); state not logged | try 1 timed out, connected at 61 s | same as C | **Wi-Fi up ~20 s**, BLE dropped by the camera |
| 2026-10-12 14:31 | asleep again after a 30 s idle standby | 10.6 s | same as C | **Wi-Fi up** |
| 2026-10-12 20:05 | `00 normal`, body power-cycled by hand ~20:00, Wi-Fi off | 9.5 s | same as C | ack `00`; no Wi-Fi in 90 s; BLE held until we closed |
| 2026-10-12 20:08 | same | 15 s | clock, SSID, `03`, ack `00`, `e182ec40=01` (no `02`) | same failure |
| 2026-10-12 23:19 | `06 sleep_pow_off_fast`, switch OFF by hand | try 1 timed out, connected at 63 s | same as C | body powered up (→`00`), no Wi-Fi, back to `06` ~30 s after we closed |
| 2026-10-13 10:56 | advertising, found in 1.8 s | **both 45 s connect tries timed out** | none | link never came up |

Byte diff of the successful 14:29 wake against the failed 23:19 wake (scrubbed):

```
                     14:29 (ok)                          23:19 (fail)
8d08a420             4d454930010010008001<id>            identical
cd7a71a0             4c756d697852656d6f7465              identical
read 03e23a31        <random 4 B>                        60b24f41 (per-session key)
ead55e60             <clock 14:29:4x>                     ea070a0c17140a0098fe (time only)
18345be0             <ssid, 32 B>                        identical
0d6f1880             03 -> notify 18345be1=00 -> 02      identical, ack 0.19 s
e182ec40             03                                  identical
```

So the cause is camera state, not bytes:
- **Success cases:** after a Wi-Fi `camcmd poweroff`, during the camera's post-BLE-wake standby, or within a few minutes of an earlier Wi-Fi session.
- **Failure cases:** after a hand power cycle, including state `06 _fast`. The 2026-10-11 night failures also followed a hand power cycle with Remote Wakeup ON, from `07` → `02`.

What we never see (macOS):
- Every CCCD write returns "attribute not found" (CoreBluetooth lists no descriptors before registration). We only ever received `18345be1` and `7be5fe6e`.
- We have never seen `e182ec41` (the Wi-Fi start result), `e182ec43` (camera Wi-Fi requests) or `4cf487c0` (clock request). A camera refusal of our STA request would be invisible to us.

---

## 5. Root-cause hypotheses, ranked

1. **We ignore the state-dependent purpose (high).** After a hand power-off, the S5 advertises `sleep_pow_off_fast`. In that state LUMIX Sync never requests STA: it uses purpose `05` with connect type `01` and joins the camera's own AP (fast boot). The body powers up for our `03`/`02` request but doesn't join the home network. It probably raises its SoftAP or waits for a second connection.
   - Fits: 23:19 (state 6), the 2026-10-11 night (state 7).
   - Doesn't explain 20:05 (state 0) alone.
   - Not yet checked: nobody scanned for the camera's `S5-…` SSID during a failed wake.
2. **We use one session where the app uses two (medium-high).** For a sleeping body the app connects, wakes it, then reconnects on the `wakeup` advertisement and repeats the request.
   - Run B → C is the only switch-OFF success that started from `02`, and it took two sessions.
   - Our single-session wake leaves the body in a ~30 s standby that we never use.
3. **The camera refuses, and we can't see the refusal (medium).** After a hand power cycle the camera may answer `e182ec41 = 01`, "cannot remote wakeup".
   - The help text tells the user to check "the connection access point in the Wi-Fi settings" [APK string `msg_cannot_restart_remote_wakeup`].
   - Our missing CCCDs hide it. Explains 20:05/20:08: camera on, hand-booted, ack `00`, then nothing.
4. **Hand power cycle resets the camera's "remote via network" context (medium-low).** The successes all follow a Wi-Fi session or a camcmd/BLE power transition, and Panasonic documents (for BGH1) that a hand power-off disables Remote Wakeup [doc].
5. **Smaller differences from the app (low each, but cheap to remove):**
   - unsolicited clock write before the Wi-Fi commands (the app writes it only on `4cf487c0`);
   - read order `e206a5c0` before `03e23a31` (the `K5.j` path reads `03e23a31` first);
   - we skip the `c97cf1a5` read (the app always reads it, and in purpose 3 its read callback is what triggers the SSID write);
   - extra `9c781c60` read the app never does;
   - fixed 0.3 s sleep instead of waiting for the `0d6f1880` write response (bleak `response=True` already waits);
   - no retry of a failed write (the app retries 3× at 500 ms).
   - Not differences: MEI0 id handling (same 16 bytes, which the camera accepts, else HCI 0x13 after the name write) and bonding (the app never bonds either).
6. **10:56 today (separate problem):** the camera advertised but never accepted a connection. Candidates: another central held the single BLE link (phone LUMIX Sync, or a stale macOS connection from the server's helper), or a low battery. Test with `system_profiler SPBluetoothDataType` and by checking that the phone app is closed.

### Does the camera stop advertising or accepting wake? (question 2)
- **Advertising:** it kept advertising state `06` from about 22:18 on 2026-10-12 until at least 11:30 on 2026-10-13 [S5].
- **Accepting connections:** the 10:56 connects failed, cause unknown.
- **What the app shows:** no keepalive or "stay wakeable" command. Remote Wakeup, Returning from Sleep Mode and Auto Transfer are camera menu settings. The help text says Bluetooth keeps running with the switch OFF and drains the battery [doc].
- **Registration dependence:** the camera drops an unregistered MEI0 id. The app auto-connects only to the last-connected camera (`CurrentConnectedAddress`). Purpose 5 vs 3 depends on the `_fast` flag.

---

## 6. Proposed `lumix/ble.py` changes (applied 2026-10-13, see section 8)

```diff
@@ class LumixBle:
+    async def rediscover_cccds(self) -> int:
+        """After registration, ask CoreBluetooth to discover descriptors again and
+        enable notify on the 10 LUMIX Sync chars; return how many succeeded.
+        (Before registration the S5 shows no CCCDs to macOS.)"""
+
     async def login(self) -> bytes:
         try:
             await self.register()
-            await self.read(CH_CAMERA_SSID)   # LUMIX Sync reads this next; keep the order
-            key = await self.read(CH_INITIAL_INFORM)
+            key = await self.read(CH_INITIAL_INFORM)       # K5.j order
+            await self.read(CH_CAMERA_SSID)
+            await self.read(CH_CAMERA_AP_PASS)             # app always reads it; value not kept
+            await self.rediscover_cccds()                  # see e182ec41 / e182ec43 / 4cf487c0
@@
-    async def wifi_join(self, ssid=None, purpose=PURPOSE_REMOTE, sta=True):
+    async def wifi_join(self, ssid=None, purpose=PURPOSE_REMOTE, sta=True):
         ...
-        if sta:
-            await self.write(CH_WIFI_CMD, bytes([WIFI_STA]))
-            await asyncio.sleep(0.3)
+        if sta:
+            await self.write(CH_WIFI_CMD, bytes([WIFI_STA if ack[:1] == b"\0" else WIFI_SOFTAP]))
         await self.write(CH_WIFI_START, bytes([purpose]))
+        res = await self.wait_notify(CH_WIFI_START_ACK, 5.0)    # 00 ok, 01 refused
+        if res and res[:1] == b"\x01":
+            raise BleStepError("wifi", "camera refused remote wake-up (e182ec41=01): "
+                               "it is busy or its Wi-Fi access point setting is wrong")
+
+    def on_any_notify(self, uuid, data): ...   # log every notify, answer 4cf487c0 with the clock
@@ async def wake(...):
-    c = LumixBle(name, scan_timeout=30, on_event=log)
+    adv = (await scan(10.0, name, stop_on_first=True) or [{}])[0]
+    state = adv.get("state_byte")
+    out["adv_state_before"] = state
+    FAST = {6, 7, 9, 10}
+    SLEEP = {1, 4, 5, 6, 7, 8, 9, 10}
+    c = LumixBle(name, scan_timeout=30, on_event=log)
     ...
-        try:
-            await c.set_clock()
-        except Exception as e:
-            log("clock: %s" % describe(e))
+        # no unsolicited clock: LUMIX Sync writes it only when 4cf487c0 asks
         step = "wifi"
-        await c.wifi_join(ssid, purpose=purpose, sta=sta)
+        if state in SLEEP:
+            # stage 1 (app: Bt Wakeup dialog): wake the body, then let it re-advertise
+            await c.wifi_join(ssid, purpose=purpose, sta=sta)
+            await c.close()
+            woke = await wait_state(name, {0, 2}, timeout=40)   # 'wakeup' or 'normal'
+            out["adv_state_after_wake"] = woke
+            c = LumixBle(name, scan_timeout=20, on_event=log)  # stage 2 (app: reconnect on 'wakeup')
+            await c.connect(); await c.login()
+            await c.wifi_join(ssid, purpose=purpose, sta=sta)
+        else:
+            await c.wifi_join(ssid, purpose=purpose, sta=sta)
+        if state in FAST:
+            out["note"] = ("camera advertised a _fast sleep state; LUMIX Sync uses its own AP "
+                           "(purpose 05, type 01) here, STA may be ignored")
```

- `wait_state()` is a small scan loop over `scan()` that returns the first state byte in the set.
- Keep the SoftAP branch (`purpose=5, sta=False`, joining the camera AP the way `first_register` does) behind a flag. It takes the Mac off the LAN.

---

## 7. Minimal real-device experiment

Run one variable per step. Stop after the first step that brings the camera onto the LAN. For each wake, log:
- the adv state before, during and after (`bin/lumix-ble scan` every 2 s);
- every notify;
- a macOS Wi-Fi scan for an `S5-…` SSID (`system_profiler SPAirPortDataType`, passive);
- an ARP/`:60606` check.

1. **Reproduce and look for the SoftAP.**
   - Close LUMIX Sync on the phone and switch the camera OFF by hand.
   - Confirm adv state `06`.
   - Run today's `wake` once. While it waits, scan Wi-Fi for `S5-…`. If the SSID shows up, hypothesis 1 is confirmed.
2. **Two-stage STA.**
   - With the same start state, do stage 1 as in step 1.
   - As soon as the scan shows `00` or `02` (within ~30 s), open a new session: registration, then SSID, `03`, ack, `02`, `e182ec40=03`.
   - Wi-Fi coming up confirms hypothesis 2.
3. **See the refusal.** Repeat step 1 with descriptor re-discovery and notify enabled after registration, and log `e182ec41` / `e182ec43` / `4cf487c0`.
4. **Control.**
   - Turn the camera ON by hand and go through the server's Wi-Fi session.
   - "Power off" from the UI (camcmd), then wake. Expected: success, as at 14:29.
5. **Setting check (only if step 1 shows `06`).** Look at Bluetooth > Returning from Sleep Mode on the camera. Set Remote priority vs Shutter priority and see whether the state byte changes `06` ↔ `04` after the next hand power-off.

User's physical steps:
- Close phone app.
- Camera switch OFF.
- Wait one minute.
- Say "ready".
- (Step 4) Switch camera ON.
- (Step 4) Pick Remote favorite.
- (Step 5) Open Bluetooth menu.
- (Step 5) Read Returning-from-Sleep value.

Wake cap: each step costs one wake (2 per 10 min). Steps 1-3 need about 4 wakes and steps 4-5 about 2, so the cap stretches the whole plan over at least 30 min. Check the battery between steps.

---

## 8. What was implemented (2026-10-13), and the real-device run

Run against the real S5 on 2026-10-13 12:19-12:27 with `tools/ble-wake-experiment.py` (results in `logs/ble-experiments/20261013-1221*.json`, `20261013-1227*.json`, gitignored; trace in that day's `/tmp/lumix-server.log`). Start state both times: switch ON, phone app closed, camera off Wi-Fi, advertising state 6 `sleep_pow_off_fast`. Two wakes were used: run 1 failed before connecting (found a bug, fixed below), run 2 woke the camera onto the LAN. notify-only and baseline were not run because run 2 succeeded.

| change | where | source | real-device verified |
|---|---|---|---|
| Scan before connecting; record and log the adv state byte (`adv_state_before`) | `ble.adv_snapshot`, `ble.wake` | section 2, `p202z5/b.java:53-85` | **yes**, with a fix. Run 1: the 10 s look saw nothing, because a camera in state 6 advertises in ~10 s bursts about every 88 s (the tool saw state 6 at 12:19:57-12:20:07, then nothing until 12:21:25). `adv_state_before` came back None, so the wake silently fell to single-session and the 30 s connect scan missed too (`scan: camera not advertising ... after 30 s`). Fix: `ADV_SLEEP_GAP_S = 100` for the snapshot and the stage-1 connect scan (both stop on first sight). Run 2: `adv state 6 sleep_pow_off_fast` after 26 s, `wake start: mode two-session` |
| Sleep states 1 and 4-10 → two-session wake: stage-1 connect + login, disconnect, poll the advertisement every ~1 s for up to 40 s until state 0/2, reconnect, log in, STA join | `ble.wake`, `ble.wait_state` | `HomeActivity.java:1230-1259` | **yes** (state 6). Stage 1: connect took 80 s (try 1 timed out at 45 s, try 2 connected 12:25:08), registration + INITIAL_INFORM read, disconnect. Login alone woke the body: adv went not-advertising, then the tool saw `2 wakeup` and the server `0 normal` 31 s later (12:25:40). Stage 2 connected in 15 s and sent SSID, `03`, ack `00`, `02`, `e182ec40=03` at 12:25:57.9; TCP 60606 answered by 12:26:10 (wake end `ok=True wifi_up_s=168.4`). STA worked from a `_fast` state, so the purpose-05 note in the result is not a blocker for this body |
| Re-find the camera by name or the manufacturer-data address bytes, not the OS address (it rotates) | `ble.adv_matches`, `scan(mfr_addr=)` | [S5] 2026-10-12 23:22; `HomeActivity.java:1234-1240` (publicAddress; that it is these bytes is [?]) | partly: name matching found it every time (`found S5-XXXXXX`); the mfr-address path was not separately exercised |
| `--single-session` keeps the old one-connection shape; `--legacy` also keeps the old bytes (baseline) | `lumix/blecli.py` | — | no (not run; run 2 succeeded first). Run 1 went single-session by accident (state None) and never reached connect, so it says nothing about the old shape |
| No unrequested clock write; clock written only when `4cf487c0` notifies | `LumixBle._on_notify` | `I5/g.java:389-391` | **yes** for "no unrequested write": no clock write in either session of run 2. `4cf487c0` never notified (its CCCD stays refused), so the clock answer path is untested |
| Login reads INITIAL_INFORM, camera SSID, AP password, in that order (password kept in memory only, never logged) | `LumixBle.login` | `K5/j.java:191-205` | **yes**: `state: logged in (initial_inform ...)` in both sessions, no error from the SSID / AP-password reads |
| After registration: rediscover services/descriptors and retry `start_notify` on the 10 chars that failed (drops bleak's stale callback first) | `LumixBle.rediscover_cccds` | `I5/g.java:1126-1140`; macOS behaviour is [?] | **no, it doesn't work on macOS.** `services rediscovered`, then all 10 `still refused (cccd 0)`: CBATTErrorDomain Code=10 "attribute could not be found". `notify subscriptions after registration: 0/10 on` in both sessions. Registration does not make the CCCDs visible to CoreBluetooth |
| `wifi_join`: no fixed 0.3 s sleep; connect type 02 on ack `00`, `01` (only with `--softap`) otherwise; 3 tries 500 ms apart | `LumixBle.wifi_join` | `I5/g.java:333-366, 471-480` | **yes**: `18345be1 00 (join ack: STA)` then `0d6f1880 02` 0.24 s later, no retries needed |
| Wait up to 10 s for `e182ec41`; `01` → `BleRefused("camera refused remote wake-up (camera state)")`, JSON `refused` + `hint`, shown in the web UI and printed by the CLI | `wifi_join`, `error_fields`, `server/static/app.js` | `I5/g.java:497-509`, `HomeActivity.java:1097-1121` | **no**: `wifi start result: none in 10 s` (no subscription, see above). The wake still worked; on macOS this wait only adds 10 s. The refusal path is untested |
| SoftAP branch (purpose 05, connect type 01, then this Mac joins the camera AP and rejoins home) only behind `--softap`; never used by the server | `wifi_join`, `ble._softap_check` | `I5/g.java:936-967`, `HomeActivity.java:1449-1458` | no (not run) |
| Every notify, state change and step logged as `ble.trace <time> +<s> ...`; the server logs lumix-ble stderr as `ble: ...` in `/tmp/lumix-server.log` | `ble.trace_line`, `LumixBle.ev` | — | **yes**: every step above comes from `grep ble.trace /tmp/lumix-server.log` |
| Server wake takes `{"variant": "two-session"\|"notify-only"\|"baseline"}`; default is two-session; timeout 480 s | `server/ble_api.py` | — | **yes**: `POST /api/ble/wake {"variant": "two-session"}` → HTTP 200, `mode two-session`; the cap counted both runs (`recent` 0 → 1 → 2). Afterwards one connect: `waiting_approval` 12:26:11 → `ready S5-XXXXXX connected` 12:26:53, a single handshake; one `POST /api/capture` → `count_changed: true`, 1844 → 1843. Camera left connected |
| `wake`: snapshot and stage-1 connect scan wait up to 100 s (`ADV_SLEEP_GAP_S`) for a sleeping camera's next advertising burst | `ble.wake` | [S5] 2026-10-13 12:19-12:21 | **yes** (run 2) |

### Follow-ups after the real run (2026-10-13, unit-tested, not yet re-run on the camera)

- `wifi_join`: when no notify subscription succeeded (0/10 on macOS), the 10 s `e182ec41` wait is skipped and logged as `wifi start result: not awaited (0 notify subscriptions on this host, so a refusal can't be observed)`. A host whose subscriptions work still waits and still raises `BleRefused` on `01`.
- Pre-scan: `adv_snapshot` keeps scanning for up to `ADV_SLEEP_GAP_S` until an advertisement carries the state byte. If the state is still None, `wake` uses two-session anyway and logs `WARNING: no advertised state ...` (result field `note_state`). That is safe for an awake camera: stage 1 is connect + login + close with no Wi-Fi or power write (the same login a single-session wake does first), and `wait_state` returns on the first ~1 s scan that shows 0/2, so an awake body costs one extra connection.
- UI: the wake progress text says it can take up to ~3 minutes (run 2 took 168 s).

### Experiment tool

`tools/ble-wake-experiment.py --variant baseline|two-session|notify-only` (run with `.venv-ble/bin/python`).

- **BLE link: through the server.** The tool never connects to the camera itself. It calls `POST /api/ble/wake` with the variant, so it shares the server's BLE lock and the same in-memory wake cap (2 per 10 min) as the UI and watchdog. It refuses to start when the server is unreachable, its BLE worker is busy, or the cap is used up. A restarted server starts with an empty cap.
- While the wake runs it logs the adv state every 2 s (passive scan), runs `system_profiler -json SPAirPortDataType` about every 5 s, and checks the camera on the LAN (TCP 60606 on the configured camera IP, `arp -n` from the cache; no other host is contacted).
- Before the wake it scans up to `ADV_SLEEP_GAP_S` (100 s) for the advertised state, the same window as the server's pre-scan.
- Wi-Fi scan limits: without Location permission macOS shows `<redacted>` for every SSID, so an `S5-…` match only works when that permission exists. Networks that appear during the wake by channel/PHY/security are logged per scan as `fingerprint_churn` but are not a signal (see below) and are not in the summary. Other networks' names are never stored.
- Result: `logs/ble-experiments/<time>-<variant>.json` (gitignored); the step-by-step trace is in `/tmp/lumix-server.log` (`grep ble.trace`).
- Each run costs one wake from the cap.

Real-device notes (2026-10-13):
- The tool's own pre-wake snapshot was 6 s, so its `adv_state_before` was None from state 6 in both runs; it now uses the 100 s window.
- The "new Wi-Fi network" fallback fired at ~35 s in both runs, including the run that never connected, so it was neighbour churn, not the camera AP. It was dropped from the summary and relabelled `fingerprint_churn` in the event log. No `S5-…` SSID was seen (SSIDs are redacted without Location permission).
- Open: e182ec41/e182ec43/4cf487c0 stay invisible on macOS; the switch-OFF start case has not been run with this code yet.

Not changed: `emulator/` has no BLE model; the BLE camera stub lives in `tests/test_ble_session.py`, extended only with behaviour cited above.

---

## Sources
- Panasonic LUMIX Sync help, Remote Wakeup (Camera Power Operations): https://av.jpn.support.panasonic.com/support/global/cs/soft/lumix_sync/en/DC-S5M2/adv_rem_wu.html
- Same page for DC-BGH1 (power-button note): https://av.jpn.support.panasonic.com/support/global/cs/soft/lumix_sync/en/DC-BGH1/adv_rem_wu.html
- LUMIX Sync Wi-Fi connection help: https://av.jpn.support.panasonic.com/support/global/cs/soft/lumix_sync/en/connect_wf.html
- Panasonic S-series help, operating the camera with a smartphone (Returning from Sleep Mode, Remote Wakeup + Auto Transfer): https://help.na.panasonic.com/answers/how-to-operate-the-camera-with-a-smartphone-lumix-s-series
- GH6 manual, remote operations: https://eww.pavc.panasonic.co.jp/dscoi/DC-GH6/html/DC-GH6_DVQP2441_eng/0126.html
