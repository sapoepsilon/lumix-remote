# LUMIX Sync Wi-Fi/HTTP flow (from the Android APK)

Source: Panasonic LUMIX Sync for Android **2.0.17** (versionCode 20017009, package `com.panasonic.jp.lumixsync`, minSdk 30), downloaded 2026-10-11 from APKPure (sha256 `6986e22f92c9d7a468ee56e948a6e1960128989a8bd90013231b9a27ab10e377`), decompiled with jadx. The APK was never installed or run. Everything below is a protocol fact read from the decompiled code (class names are jadx-obfuscated, given only as pointers), not copied code. BLE is covered elsewhere; this file is Wi-Fi/HTTP only.

Where code paths are obfuscated the meaning is inferred from control flow and the app's own log strings; those spots are marked [inferred].

## 1. Transport facts

- All camera control is `GET http://<ip>/cam.cgi?mode=<m>[&type=<t>][&value=<v>][&value2=<v2>]` (port 80). The app builds the query in exactly that order and never adds other parameters.
- Every cam.cgi request carries `User-Agent: LUMIX Sync`. If the accctrl reply carried a 5th CSV field, it is sent back as `X-SESSION_ID` on every request (the S5 sends no 5th field, so no header). Thumbnail/medium downloads add `X-CONVERT: SmallSize` or `X-CONVERT: MediumSize`.
- Default connect/read timeout 10 s; `camcmd`, `asst_disp`, `pinch` and generic menu commands use 3 s; the `ddd` fetch uses 3 s with 5 retries 1 s apart.
- One request at a time: all camera commands go through a global lock, so the app never has two cam.cgi requests in flight.
- Reply type is chosen by Content-Type: `text/xml` is parsed as `<camrply><result>..`, `text/plain` as CSV (`accctrl`, `getinfo&type=lens`).
- Other user agents: Java CDS Browse POST uses `Panasonic MIL DLNA CP UPnP/1.0`; the native DLNA lib (libdlnaCore.so, used for SUBSCRIBE and the native browse) uses `Panasonic Android/1 DM-CP`; the app's own DMS answers as `Android/1 UPnP/1.0 Panasonic-DM-SV/1`.

### Generic retry rule (very important for err_busy)
Every wrapper retries on `err_busy` with a 1 s sleep, at most 5 attempts (`camcmd`, `camctrl`, `setsetting`, generic menu command, `startstream`, `stopstream`, `abortsenddata`). `accctrl` also retries on `err_reject`. After 5 tries the last error is returned to the UI and the app does nothing more automatically. Exceptions: `camctrl` touch-drag/AF-AE-lock variant tries once; `getstate` tries 3 times.

## 2. Model classification from `ddd`
`http://<ip>:60606/Lumix/Server0/ddd` is parsed for friendlyName, manufacturer, modelName, modelNumber, `X_AdditionalFunction`, `X_FirmVersion`, `X_CamCategory`, `X_CamCapability`, serialNumber, UDN and the service list (serviceType, SCPDURL, controlURL, eventSubURL).

- Manufacturer must be `Panasonic`.
- `X_AdditionalFunction=CPRemoteView` + `X_CamCategory=MirrorlessILC` = "LUMIX camera" class (S5 is this). `MovieRemote` is a separate class. Only this class gets the Cam_Sync busy tracking, lens info and getstate watchdog.
- **Encrypted handshake is used only if `X_CamCapability` ends in `1`.** The S5 ddd has no `X_CamCapability`, so LUMIX Sync uses plain `req_acc` on the S5 (matches our probe: `req_acc_g` -> err_param).
- S5 ddd also has `X_PTPPortNo=15740` (PTP/IP port), which LUMIX Sync does not use for remote shooting.

## 3. Handshake (accctrl)

Values:
- `value` = `4D454930-0100-1000-8001-` + a 12-hex-char seed generated once by the native DLNA lib and stored in prefs (`Dlna_UUID_Seed`). Same prefix as ours.
- `value2` = device name (user-set "LUMIXSync.Network.Name", default Android `Build.MODEL`), fully percent-encoded (space `%20`, every reserved char escaped).
- Encrypted models: first `accctrl&type=req_acc_g` -> `ok,<8 hex>` nonce (parsed as a 32-bit int). Then both strings are transformed: pad with 0xFF chars to a multiple of 4, take each 4-char block as a big-endian uint32, XOR with the nonce, byte-swap, print as `%08x`. Nonce 0 is replaced by the constant -1875740628. The request is then `req_acc_e&value=<hex uuid>&value2=<hex name>`.

Loop (`accctrl&type=req_acc`): repeat every **1 s** while the reply is `ok_under_research` (camera shows an approve prompt) or `ok_under_research_no_msg` (no prompt, auto-approve pending). Stop on anything else. Reply fields: `<result>,<camera name>,<mode>,<enc>[,<session id>]`; mode can be `remote`, `upload`, `upload_bt`, `transfer`. If the user cancels while waiting, the app sends `accctrl&type=req_acc_can&value=<uuid>`.

accctrl results handled: `ok`, `ok_under_research`, `ok_under_research_no_msg`, `err_others_requesting` (another phone is pairing), `err_user_refused` (user said no on the camera), `err_param`, `err_reject`, `err_non_support`, `err_already_connected`, `err_unsuitable_app` (silently ignored in the reconnect loop; just retried 5 s later). An empty reply ("ret_empty") means "no answer, probably wrong Wi-Fi".

## 4. Full connection sequence (what the app does after req_acc = ok)

From the connect thread [service `c.w()` + `a.c`]:
1. (Encrypted models only, before step 2) `setsetting&type=device_name&value=<name>`.
2. **UPnP GENA SUBSCRIBE** (native lib) to the eventSubURL of the service whose type contains `ConnectionManager` (S5: `/Server0/CMS_event` on :60606), callback path `/Camera/event` on an app-chosen port, `NT: upnp:event`, `TIMEOUT: Second-300`, `User-Agent: Panasonic Android/1 DM-CP`. The lib has SID-based templates (`SID: uuid:%s`, `TIMEOUT: Second-%d`) and a `DM_UPNPD_SubscribeReNew` routine, so it renews with the SID [inferred: renewal cadence not decoded].
3. Sleep **200 ms**.
4. `setsetting&type=raw_img_send&value=enable|disable` (app setting "send RAW").
5. `getinfo&type=capability` (must be `ok` or the connect fails, then UNSUBSCRIBE).
6. `getinfo&type=allmenu` (must be `ok`).
7. `getinfo&type=curmenu`.
8. LUMIX-class cameras: `getinfo&type=lens`; if capability has `touch_command_auto`: `getsetting&type=touch_type`.
9. `getstate` (3 tries); if `<temperature>high` the connect returns "hightemp".
10. Native DMP is pointed at the camera (for browsing later).
11. Non-encrypted reconnect path: `setsetting&type=device_name&value=<name>` (sent after step 10, every reconnect).
12. Start the **CameraWatchLoop** (see 5). The app does **not** send `recmode` here. `recmode` is only sent when the live-view screen starts its stream (section 7).

Note the order: SUBSCRIBE happens first, while a freshly connected S5 is still in play mode, and the camera gets 1-2 s of read-only getinfo traffic before anything changes mode.

## 5. Keepalive / watchdog

- **CameraWatchLoop**: every **1 s** (sleep 1000 ms) -> `getstate` (up to 3 tries; on `err_busy` sleep 1 s and retry). If getstate returns any other error (e.g. `err_reject`) or no reply, or if the last successful command is more than 30 s old, the app declares the camera **disconnected**: it UNSUBSCRIBEs, drops the session, and the reconnect loop takes over (every 5 s: SSDP search, then the full handshake + section 4 again).
- On every Cam_Sync event it also refreshes `getinfo&type=lens`, `getsetting&type=ex_tele_conv` and (if touch_command_auto) `getsetting&type=touch_type`.
- **Held shutter / burst / bulb keepalive**: while the shutter is held (burst/bulb), the watch loop is paused and the live-view model sends `getstate&type=keep_alive` every **500 ms** instead; normal watch resumes after release.
- The live-view UDP socket has a 1 s receive timeout but the stream is **not** restarted on timeouts (current path). There is an older path in the same client class that re-sends `startstream` after each 1 s UDP timeout; it appears unused in 2.0.17.

## 6. Events (`X_Panasonic_*` via the GENA NOTIFY)

The native lib flattens each NOTIFY into `@<name>=<value>` lines. Names and values the app knows:

| Variable | Values (code) |
|---|---|
| `X_Panasonic_Cam_Sync` | `mod_Play` 201, `mod_Rec` 202, `lens_Atta` 203, `lens_Deta` 204, `busy` 205, `update` 206, `lens_Update` 207, `busy_no_msg` 208, `fan_alert_on` 209, `fan_alert_off` 210 |
| `X_Panasonic_Cam_VRec` | `start`, `done`, `mod_LoopRecStop`; errors `err_SDfull`, `err_UsrStp`, `err_UpLimit`, `err_wr`, `err_SpdInsuf`, `err_AVCHDTr`, `err_Tempa`, `err_MemFull`, `err_CrFold`, `err_Tempa_Multi` |
| `X_Panasonic_Cam_PictBrst` | `SD_wr_fin`, `PictBrst_End`; errors `err_SDfull`, `err_UsrStp`, `err_comm`, `err_BufFull`, `err_CrFold`, `err_wr`, `err_Tempa_Multi`, `err_Tempa_4kPreBrst_to_4kBrst`, `err_Tempa_6kPreBrst_to_6kBrst`, `err_Tempa_PreRec_to_Disable`, `err_Backup_Rec_Sdcard_Differ`, `err_Wifi_Connecting` |
| `X_Panasonic_Cam_PRec` | `err_rec`, `err_wr`, `err_CrFold`, `err_DisableShoot`, `err_Wifi_Connecting`, `err_Tempa_Multi` |
| `X_Panasonic_Cam_Req` | `mod_Rec` 401, `mod_LoopRecStop` |
| `X_Panasonic_Cam_Transfer` | `transfer_Start` 501, `transfer_Stop` 502 |

What the app does with Cam_Sync (LUMIX-class cameras only):
- A small state machine keeps two flags: **cameraBusy** (`busy`/`busy_no_msg` -> true, `update` -> false) and **lensAttached** (`lens_Atta`/`lens_Update` -> true, `lens_Deta` -> false). Only transitions are forwarded to the UI.
- `busy` (205): the live-view screen **stops the live-view stream**, closes the app's menus, and shows a non-cancellable dialog "Camera operation is in progress." The thumbnail loader is paused. Nothing is sent to the camera except the 1 Hz getstate.
- `busy_no_msg` (208): same busy flag, no dialog.
- `update` (206): clears the busy flag (thumbnail loading resumes); no UI action by itself.
- `lens_Atta` (203): closes the busy dialog and **restarts live view**: stopstream (if any) -> `camcmd recmode` -> `startstream`, while the watch service refreshes `getinfo&type=lens` / `ex_tele_conv` / `touch_type` (curmenu is re-read only in the "smartphone priority" variant). This is the app's "camera is usable again" signal.
- `mod_Play` (201): **ignored** on the live-view screen (no dialog, no recmode). The app does not try to pull the camera back to rec.
- `mod_Rec` (202): on the home screen it auto-opens remote live view; on the live-view screen it only closes menus.
- `lens_Update` (207): firmware-update prompt for the lens. `fan_alert_on/off`: overheat fan dialog.
- `transfer_Start`/`transfer_Stop`: camera-initiated image transfer (app shows receive UI, answers `notify&type=transfer&value=ok|ng|cancel`).

## 7. Live view

Start sequence (live-view screen create/resume, and after `lens_Atta`):
1. Stop any old stream thread: `stopstream` (5 tries on err_busy).
2. Bind a UDP socket on the first free port from **49152** upward, SO_TIMEOUT 1 s, 64 KB buffer.
3. `camcmd&value=recmode` (or `pictmode` on some old bodies), 5 tries on err_busy. Results:
   - `ok` -> continue.
   - `wifi_reset` -> the camera is restarting Wi-Fi; the app waits up to 120 s for the camera's SSDP alive (same UUID), then continues.
   - `ok_shoot_mode_changed` -> continue, UI notified that the shooting mode changed.
   - anything else (incl. `err_busy` after 5 tries) -> live view error state, no further retries until the screen resumes.
4. `startstream&value=<udp port>` (no value2), 5 tries on err_busy.
5. Receive loop. A packet-type field in the frame header selects the kind: 33 = image frame, 97 = other data (offset not decoded here). No resend of startstream while running.
6. On leave: `stopstream`.

## 8. Shooting command sequences

Photo (single):
- If MF assist / pinpoint / digital scope is open: `camctrl&type=asst_disp&value=off&value2=<mf_asst|pinpoint_af|digital_scope>/0/0` first; if touch AE is active, turn it off (`camctrl&type=touchae_ctrl&value=off`).
- `camcmd&value=capture`. Result errors other than `err_noremain` are not shown.
- Touch-shutter variant: `camctrl&type=touch&value=<x>/<y>&value2=on` takes the picture; cancel with `camctrl&type=touch&value=0/0&value2=off`. Enable/disable touch-shutter mode: `camctrl&type=touchcapt_ctrl&value=enable|disable|off`.
- Burst/held shutter: `capture` (or touch on), start the 500 ms `getstate&type=keep_alive` loop, release with `camcmd&value=capture_cancel`.
- After capture, when the app leaves live view or opens a menu while auto-review may be on: `camcmd&value=autoreviewunlock`.

Video:
- (touch AE off if active) `camcmd&value=video_recstart`; stop `camcmd&value=video_recstop`. Progress via getstate `<rec>`, `<progress_time>` and `X_Panasonic_Cam_VRec` events.
- video_recstart error mapping: `err_busy` = "recording is in progress on the camera" (card write still running), `err_reject` = "not possible in the current mode", `err_noremain`, `err_temperature`, `err_backup_rec_sdcard_differ`, `err_crfold`, `err_wifi_connecting`, `err_sdcard_access`, `err_cannot_record_avchd_on_xqd`, `err_prores_sd_disable`, `err_no_media`.

Result-code buckets used by the live-view model: `err_critical`/`err_param`/`err_system_error` = generic failure; `err_sdcard_write_protected`; `err_noremain`; `err_reject`; `err_busy`; `err_temperature`; plus the ones above.

## 9. Touch AF / focus

- Capability `touch_command_auto` makes the app read `getsetting&type=touch_type` (bitmask). If bits 0 **and** 1 are set, taps/drags go straight to the camera as touch commands. Otherwise (S5 reports `5` = bits 0 and 2) tap = `camctrl&type=touch&value=<x>/<y>&value2=on`, except in the 1-area/pinpoint AF case where the app opens the MF/pinpoint assist view instead. Coordinates are 0..1000.
- Release/cancel: `camctrl&type=touch&value=0/0&value2=off` (the app always uses 0/0 for off), `camcmd&value=touchafrelease`, `touchaerelease`, `touchafaerelease`, `touch_af_cancel`, `touch_ae_cancel`, `af_area_reset`.
- Drag (AF point move): `camctrl&type=touch_trace&value=start|continue|stop&value2=<x>/<y>`. Move AF point: `camctrl&type=touchaf_chg_pos&value=<x>/<y>`. AF area size: `camctrl&type=touchaf_chg_area&value=up|down`, or pinch `camctrl&type=pinch&value=start|continue|stop&value2=<x1>/<y1>/<x2>/<y2>` (S5 capability `af_size_change_pinch`).
- One-shot AF: `camcmd&value=oneshot_af` (capability `oneshot_af_enable`).
- Touch AE: `camctrl&type=touchae_ctrl&value=on|off`. AF/AE lock: `camctrl&type=af_ae_lock&value=on|off`.
- Assist display: `camctrl&type=asst_disp&value=<full|pinp|current|current_auto|move|off>&value2=<mf_asst|pinpoint_af|digital_scope>/<x>/<y>`; magnification `camctrl&type=change_disp_mag&value=<mag*100>` (S5 range in capability `<asst_disp>`).
- Focus by wire: `camctrl&type=focus&value=tele-normal|tele-fast|wide-normal|wide-fast|focus-stop`.

## 10. Interval / stop motion / other remote modes

- Interval: drivemode set via the allmenu command, then `camctrl&type=interval&value=set` (repeats every 1 s while reply is `ok_under_research`), then shutter. Control: `camctrl&type=interval&value=pause|restart|continue|cancel|stop`. State in getstate: `<interval_status>`, `<interval_remain_num>`, `<interval_end_time>`, warn_disp `interval_canceled|interval_fail|interval_finish|interval_ng_mode|interval_pause`.
- Real DC-S5 fw 2.03 (2026-10-12): with drive mode `stopmotion_interval`/`interval` getstate already says `interval_status=enable`, and `camctrl interval set` then answers `err_reject`. The shutter alone (`camcmd capture`) starts the run. Running = `interval_status=rec` (not `run`), `pause` (+ warn `interval_pause`), `restart` resumes with a shot right away, `stop` -> `enable`; a finished run shows `off` + `interval_finish` for ~5 s, then `enable`.
- Stop motion: `camctrl&type=stop_motion&value=<op>`; the app sends `value=pause` when opening a menu while getstate `<stop_motion>` is `auto`.
- Live composite `camctrl&type=livecomp&value=<op>`, high-res `camctrl&type=hrs&value=<op>`, program shift `camctrl&type=program_shift&value=<dir>`, shutter sync `camctrl&type=shtrspeed_syncro&value=..&value2=..`, `camctrl&type=frame_ctrl&value=..`, 4K photo marking `camcmd&value=4kphoto_marking`.

## 11. Settings

- All settings except a handful are sent **verbatim from allmenu**: each menu item's `cmd_mode`/`cmd_type`/`cmd_value` (+ optional value2) becomes `cam.cgi?mode=<cmd_mode>&type=<cmd_type>&value=<cmd_value>[&value2=..]`. On GH5/GH5S/G9 drivemode needs value2 from curmenu; S-series sends what allmenu says.
- After every successful setting: `getinfo&type=curmenu` to refresh state.
- The setting reply may contain `<closemenu mode="recmode|pictmode|playmode"/>` (the camera changed mode as a side effect, so the app closes its menu; `playmode` sends the app to the playback screen) and `<dispmsg id=".." type=".."/>`. **We should log and honour these.**
- Direct setsetting calls in code: `device_name`, `raw_img_send`, `current_sd`, `photostyle`, `photostyle2`, `colormode`, `peaking`, `overlay`.
- Direct getsetting calls: `touch_type`, `ex_tele_conv`, `qmenu_disp_style`, `play_sort_mode`, `photostyle`, `photostyle2`, `colormode`.
- Menu notify (capability `menu_notify_enable`, present on the S5): the app sends `camcmd&value=menu_entry` when it opens its camera-settings menu and `camcmd&value=menu_exit` when it closes it.
- `camcmd&value=lcd_on` is sent after live-view resume/menu actions to wake the camera monitor.

## 12. Playback and transfer

- Enter: stop live view, `camcmd&value=playmode` (5 tries on err_busy), `getstate`, then `get_content_info` (`<total_content_number>`, `<current_position>`); `err_db` = camera database not ready.
- Browse: SOAP POST to `http://<ip>:60606/Server0/CDS_control`, `SOAPAction: urn:schemas-upnp-org:service:ContentDirectory:1#Browse`, `Content-Type: text/xml; charset="utf-8"`, standard `u:Browse` (`BrowseDirectChildren`, `Filter=*`, empty SortCriteria) plus `<pana:X_FromCP>LumixLink2.0</pana:X_FromCP>`, paged 50 items per request. The native browser also supports `pana:X_Filter` and `pana:X_Order` extension tags (rating/type filters, sort order).
- Download: plain GET of the DIDL `res` URL (port 50001), `User-Agent: LUMIX Sync`, optional `X-CONVERT: SmallSize|MediumSize` for camera-side resize.
- Rating: `editcmd&type=rating&value=..`. Movie playback on camera: `playcmd&type=start|stop|pause|restart|setplayscene`.
- Camera-initiated transfer: Cam_Transfer events + `notify&type=transfer&value=ok|ng|cancel`.
- Leaving playback back to live view = the live-view start sequence (section 7: stopstream, recmode, startstream). There is no special "exit playback" command.
- Firmware/data upload (not needed by us): `requestsenddata`, `startsenddata`, `senddata` (multipart POST, UA `LUMIX Sync`), `endsenddata`, `abortsenddata`, `getprogress`, `camctrl&type=fw_update_mode|lens_fw_update_mode`, `getinfo&type=camsetting` (binary).

## 13. getstate fields the app reads
`cammode` (rec/pict/play), `rec`, `progress_time`, `remaincapacity`, `recremaincapacitysec`, `video_remaincapacity`, `sd_memory`, `sd2_memory`, `sd_access`, `sd2_access`, `sdcardstatus`, `sd2_cardstatus`, `sd_full`, `sd2_full`, `current_sd`, `slotfunc`, `temperature` (`high` = disconnect for heat), `batt` (`a/b`), `operate` (two `enable/disable` halves: remote operation allowed / second flag), `interval_*`, `stop_motion`, `lc_*`, `hrs_*`, `lens`, `play`, `warn_disp` (values seen in code: `battery_degradation`, `memory_card_heat_cannot_rec`, `memory_card_heat_detect`, `card_not_supported`, `heat_can_use_camera`, `heat_warn_cannot_use`, `heat_warn_card_temperature`, `media_over2tb`, `request_to_use_ssd`, `charge_bracket_cancel`, `hrs_synthesis_failed`, `image_stabilizer_on`, `interval_*`, `live_composite_ready|start`, `focus_select_lense_af`, `rec_stop_to_change_focus`, `no_disp`), `bt_auto_trans`, `usb_ssd`, `cinelike`, `video_priority_disp`, `mf_guide`, `crop_zoom_focaldist_max`. A numeric field equal to 4294967295 is treated as err_busy.

## 14. Complete command inventory found

`camcmd&value=`: `recmode`, `playmode`, `pictmode`, `capture`, `capture_cancel`, `video_recstart`, `video_recstop`, `autoreviewunlock`, `menu_entry`, `menu_exit`, `lcd_on`, `oneshot_af`, `touchafrelease`, `touchaerelease`, `touchafaerelease`, `touch_af_cancel`, `touch_ae_cancel`, `af_area_reset`, `4kphoto_marking`, `alert_off`, `poweroff`, `tele-normal`, `tele-fast`, `wide-normal`, `wide-fast`, `zoomstop`.

`camctrl&type=`: `touch`, `touch_trace`, `touchaf_chg_pos`, `touchaf_chg_area`, `pinch`, `touchcapt_ctrl`, `touchae_ctrl`, `af_ae_lock`, `asst_disp`, `change_disp_mag`, `focus`, `interval`, `stop_motion`, `livecomp`, `hrs`, `program_shift`, `shtrspeed_syncro`, `frame_ctrl`, `fw_update_mode`, `lens_fw_update_mode`.

`setsetting&type=`: `device_name`, `raw_img_send`, `current_sd`, `photostyle`, `photostyle2`, `colormode`, `peaking`, `overlay`, plus everything in allmenu (`cmd_type` values, e.g. our dump has 359 distinct cmd_values).

`getsetting&type=`: `touch_type`, `ex_tele_conv`, `qmenu_disp_style`, `play_sort_mode`, `photostyle`, `photostyle2`, `colormode` (+ any allmenu type).

Other modes: `accctrl` (`req_acc`, `req_acc_g`, `req_acc_e`, `req_acc_can`), `getstate` (`type=keep_alive` variant), `getinfo` (`capability`, `allmenu`, `curmenu`, `lens`, `camsetting`), `startstream`, `stopstream`, `get_content_info`, `playcmd`, `editcmd`, `notify`, `getprogress`, `requestsenddata`, `startsenddata`, `senddata`, `endsenddata`, `abortsenddata`.

S5 capability `camcmdlist` only lists `tele-*`, `wide-*`, `poweroff`, `4kphoto_marking`; the other camcmds are implicit.

## 15. Our err_busy problem: explanation and recovery

### What our log shows
Settings keep working, but `camcmd` (capture, recmode, playmode, autoreviewunlock) and often `startstream` answer `err_busy`, starting after a `mod_Play` Cam_Sync event. Our client then retries recmode and re-sends startstream every ~5 s (watchdog), and later `getstate` itself answers `err_reject`.

### What LUMIX Sync does differently
1. It never fights a camera-side mode change. `mod_Play` is ignored; `busy`/`busy_no_msg` stops live view and **stops sending camcmd** until `lens_Atta` arrives, and only then runs stopstream -> recmode -> startstream once.
2. It never re-sends startstream on a running stream and never sends recmode from a watchdog. recmode is sent once per live-view start (5 x 1 s retries max), then it gives up and waits for the next lifecycle event.
3. It SUBSCRIBEs exactly once per session, right after req_acc and before any mode change, then spends 1-2 s on read-only getinfo calls. It never re-subscribes on a live session; if anything goes wrong it tears the whole session down.
4. Its watchdog is strict: getstate `err_reject` (or 3 failed/busy tries, or 30 s without a successful reply) = disconnected -> UNSUBSCRIBE -> full reconnect (req_acc, SUBSCRIBE, raw_img_send, capability, allmenu, curmenu, lens, touch_type, getstate, device_name).
5. It sends `User-Agent: LUMIX Sync` (we send `LumixRemoteMac/0.1`; the camera reports `appname LUMIX_Sync` in capability and may key behaviour on it [unverified]).

### Hypothesis
`err_busy` on camcmd with working setsetting means the S5 holds its "remote shooting" lock elsewhere: the camera is in a body-side state (playback entered by the camera, auto-review, card write, body menu) and announces that with `mod_Play` and/or `busy`. In that state every mode/shutter command is refused with err_busy, while settings writes are still accepted. LUMIX Sync waits for the camera to release the lock (`update`, then `lens_Atta` when it is back in rec and ready). Our client instead keeps sending recmode/startstream/capture into the busy window, and the re-subscribe path (SUBSCRIBE while in rec, recmode during the camera's own switch -> `err_critical`) can leave the camera's remote state machine stuck so that it never emits `lens_Atta` again. The later `getstate err_reject` shows the camera eventually dropped our session; LUMIX Sync would have reconnected from scratch at that point, while our client keeps using the dead session.

Secondary contributors worth removing: periodic startstream resends (every ~5 s, alternating ok/err_busy in our log), `recmode` sent immediately after touch_type with no getstate in between, touch off sent with real coordinates instead of `0/0`, and the extra 2 Hz getsetting focal/shtrspeed + 15 s curmenu polling (LUMIX Sync only polls getstate at 1 Hz and reads curmenu after changes/events).

### Recommended recovery sequence (mirrors LUMIX Sync)
1. Handle Cam_Sync like the app: `busy`/`busy_no_msg` -> stop live view (stopstream once), stop all camcmd/startstream, keep 1 Hz getstate, show "camera in use". `update` -> clear busy. `lens_Atta` -> clear busy and run the live-view start once: `stopstream` -> `camcmd recmode` (up to 5 tries, 1 s apart) -> `startstream`. Then `getinfo lens` (and `getinfo curmenu` to resync our UI).
2. `mod_Play` alone -> do nothing automatic (mark mode=play, keep live view off). Do not send recmode in response. Only send recmode on an explicit user action (or on `lens_Atta`/`mod_Rec`), once, with the 5 x 1 s budget.
3. Never re-send startstream while a stream is supposed to be running; if frames stop, treat that as a lifecycle event (wait for an event, or do one full live-view restart after the camera is not busy).
4. On `err_busy` from camcmd: retry the same command up to 5 times, 1 s apart, then surface the error and stop. No other commands in between (single in-flight request).
5. If `getstate` answers `err_reject`, fails 3 times, or nothing succeeded for 30 s: treat as disconnected. `stopstream` (best effort), UNSUBSCRIBE, then full reconnect exactly in the app's order: `ddd` -> `accctrl req_acc` (1 s loop on ok_under_research*) -> SUBSCRIBE (camera should be in play right after a fresh accept) -> wait 200 ms -> `setsetting raw_img_send` -> `getinfo capability` -> `getinfo allmenu` -> `getinfo curmenu` -> `getinfo lens` -> `getsetting touch_type` -> `getstate` -> `setsetting device_name` -> start 1 Hz getstate -> only then the live-view start (stopstream, recmode, startstream).
6. If the camera is still wedged after a full reconnect (camcmd err_busy, no events), it is not something the app can recover over Wi-Fi either; the app has no "unlock" command beyond the ones listed (`autoreviewunlock`, `menu_exit`, `capture_cancel`, `touch*release`). Worth one test each before power-cycling: `camcmd menu_exit`, `camcmd capture_cancel`, `camcmd autoreviewunlock`.
7. Send `User-Agent: LUMIX Sync` on cam.cgi and `Panasonic Android/1 DM-CP` on SUBSCRIBE to remove UA as a variable.

### Tests to confirm on the S5
- Log every NOTIFY with timestamps next to every cam.cgi request, and check whether `busy`/`update`/`lens_Atta` bracket the err_busy window.
- Press Play on the body during remote: expect `busy` + `mod_Play`; press half-shutter: expect `update`/`lens_Atta`/`mod_Rec`. Check whether camcmd is err_busy exactly between them.
- Repeat the failing scenario with the app-like client (no watchdog startstream, no recmode on mod_Play) and see if the wedge still happens.
- Check if any setsetting reply carries `<closemenu mode="playmode"/>` right before a `mod_Play`.
