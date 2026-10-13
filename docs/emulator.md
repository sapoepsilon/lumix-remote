# Camera emulator (`emulator/`)

A fake LUMIX DC-S5 that speaks the same Wi-Fi protocol as the real body (cam.cgi, UPnP ddd/ContentDirectory/GENA, :50001 files, UDP live view) and reproduces the failure modes we hit on 2026-10-11, so the server and UI can be developed and QA'd without a camera. Built from the recordings in `probe-results/` (gitignored) and `probe-results/FINDINGS.md`; identifiers are replaced with fake ones.

## Server requirement (for the multi-camera refactor) — read first

The emulator cannot listen on port 80 without root, and several emulators on one machine need distinct ports. The server must let each camera override its camera-side ports and must not assume one camera per IP:

1. **Per-camera port map** `{http, upnp, files}` (defaults `80 / 60606 / 50001`), used everywhere the client talks to the camera:
   - cam.cgi: `http://<ip>:<http>/cam.cgi?...` (omit `:80` for real bodies).
   - ddd `GET :<upnp>/Lumix/Server0/ddd`, GENA `SUBSCRIBE/UNSUBSCRIBE :<upnp>/Server0/CMS_event`, Browse `POST :<upnp>/Server0/CDS_control`, discovery probes of `:<upnp>`.
   - Downloads: the camera's own `<res>` URLs carry its port, but the client rebuilds them from `cam.ip`; use `cam.ip:<files>` there. (The emulator's `<res>` URLs already carry the right host:port, so trusting the URL port also works.)
   - CLI flags + `cameras.json` `ports`, e.g. `--camera-ip 127.0.0.1 --camera-http-port 8080 --camera-upnp-port 60606 --camera-files-port 50001`. The WIP tree on 2026-10-11 23:30 already has these flags and `CameraConfig.http_port/upnp_port/files_port`; with them the current server connects to the emulator end to end (connect, live view at 25 fps, file browsing, Range download, power-cycle reconnect).
2. **Per-camera client-side ports**: each camera needs its own UDP live-view port (`startstream&value=<port>`) and either its own GENA callback port or its own callback path on a shared port. The camera streams to the IP that sent `startstream`, so two cameras on one live-view port would mix frames.
3. **Don't key camera sessions by IP alone.** Two emulators on `127.0.0.1` differ only by port. Key by registry id (or `ip:http_port`), and make "claimed IPs" / discovery skip-lists port-aware, or run emulators on distinct loopback IPs (`--host-step`, below).
4. **No SSDP from the emulator.** It doesn't multicast `ssdp:alive`, so the server must be given the address (`--camera-ip` / registry entry) rather than discover it.

## Run

```sh
.venv/bin/python -m emulator                      # one S5 on 127.0.0.1: cam.cgi :8080, upnp :60606, files :50001, control :8081
.venv/bin/python -m emulator --settle 1-2         # shorter post-SUBSCRIBE wait (real: 5-22 s)
.venv/bin/python -m emulator --wedge-realism off  # never wedges
.venv/bin/python -m emulator --count 2            # two cameras: ports +0 and +10 (8080/60606/50001/8081, 8090/60616/50011/8091)
.venv/bin/python -m emulator --count 2 --host-step  # 127.0.0.1 and 127.0.0.2 on the same ports
                                                  # (macOS: sudo ifconfig lo0 alias 127.0.0.2 up first)
```

Then point the server at it:

```sh
LUMIX_REMOTE_HOME=/tmp/lumix-emu ./run.sh --port 8770 --camera-ip 127.0.0.1 \
  --camera-http-port 8080 --camera-upnp-port 60606 --camera-files-port 50001
```

Use a separate `LUMIX_REMOTE_HOME` so the emulator doesn't overwrite the real camera's remembered IP/registry and so the server's pidfile takeover doesn't kill a server that's driving the real body.

Other flags: `--model dc-s5` (the only profile), `--approval auto|prompt|refuse`, `--session-timeout 30` (0 = never), `--fps 25`, `--interval-scale 0.01` (time-lapse 100x faster), `--card-photos 4`, `--card-group 120`, `--no-range`, `--dl-rate BYTES_PER_S` (throttle :50001 so an import can be paused mid-file; also `dl_rate` via `/emu/config`), `--seed N`, `--control-port`, `--port-stride`, `-v`.

Each camera gets a fake identity: MAC `02:4C:58:E0:00:0N` (locally administered), friendlyName `S5-E0000N` (the S5 uses `S5-` + last 3 MAC bytes), serial `EMU000000N`, UDN `uuid:4D454930-0100-1000-8000-024C58E0000N`.

## What is emulated

| Surface | Behaviour |
|---|---|
| `:upnp/Lumix/Server0/ddd` | Recorded S5 ddd (firmware 2.03) with fake friendlyName/serial/UDN and URLs pointing at the emulator's host:port. UPnP `SERVER`/`DATE` headers as recorded; the camera clock starts at 2020-02-26 08:45 at every boot (the test body's clock was never set). |
| `accctrl req_acc` | First call `ok_under_research_no_msg,<name>,remote,encrypted`, `ok,<name>,remote,encrypted` once 1 s has passed (recorded 1.3-1.5 s). `--approval prompt` answers `ok_under_research` until `POST /emu/approve`. `req_acc_g/e/a` -> `err_param` (S5 has no encrypted handshake). A newly accepted client replaces the previous one. |
| Session | `text/plain` CSV for accctrl and `getinfo lens`, `text/xml` otherwise, `Server: Panasonic`, `Connection: close` on every reply. Before req_acc only `getstate`/`getinfo` answer (as recorded); everything else `err_reject`. After a session drop (idle `--session-timeout`, or `POST /emu/drop-session`) **every** command, `getstate` included, answers `err_reject` until a new req_acc. |
| `getstate` | Recorded field lists for rec and play mode; live `cammode`, `rec`, `progress_time`, `sd_access` (on during card writes), `remaincapacity`, `burst_interval_status` (on with drive mode `stopmotion_interval`), `interval_*`, `warn_disp`. |
| `getinfo` | `capability`, `allmenu` (recorded), `curmenu` (recorded, updated by setsetting), `lens` (recorded CSV); others `err_param`. |
| `getsetting` / `setsetting` | Stateful. Values from the server log (`focal 1055/256`, `shtrspeed 0/256`, `iso auto`, `afmode 1area_asst` + text `auto`, `touch_type 5`, ...). Unknown getsetting types answer `err_critical` (as recorded for `mf_assist`, `recmode`). At boot, getsetting values follow the recorded curmenu wherever it holds an allmenu value (EV +1/3, drive Time Lapse, ...), so both answer from one state. setsetting accepts every `(cmd_type, cmd_value)` in allmenu (updating the matching curmenu item; a sub-choice item such as Time Lapse Shot / Stop Motion keeps its own token), free values for `__value__` items such as `interval_shoot_num` / `interval_shoot_interval` / `interval_start_time` (stored in that item's curmenu value, which the time lapse uses), plus the direct types LUMIX Sync uses; other values `err_param`. |
| `camcmd` | `recmode`/`playmode` (mode flips 0.3 s later; recmode echoes NOTIFY `lens_Atta` + `mod_Rec`), `capture` (adds a photo, card-write busy window), `video_recstart/stop` (adds an MP4), `oneshot_af`, `capture_cancel`, `menu_entry/exit`, `autoreviewunlock`, `lcd_on`, touch releases, zoom, `poweroff`. Unknown values `err_param` (also while wedged). |
| `camctrl` | `touch` (`on` twice without `off` -> `err_reject`), `touch_trace`, `touchaf_chg_pos`, `pinch`, `asst_disp`, `focus`, `af_ae_lock`, `touchae_ctrl`, ... -> ok in rec. `interval set` answers `err_reject` (real S5: the drive mode arms the run); `capture` in Time Lapse drive starts the run; `pause/restart/continue/cancel/stop`. |
| Time lapse | Each shot lands in a new `Interval` container on the card (`<firstid>DIR`), at the curmenu interval (30 s) x `--interval-scale`, for curmenu `interval_shoot_num` shots. |
| Interval + mode switch (real S5, 2026-10-12 11:55, `tests/fixtures/real-s5-interval-restart-incident-2026-10-12.log`) | `playmode` while the run is paused -> ok (camera to play); the next `recmode` -> `err_critical` and the wedge; `setsetting` then `err_critical`, and `getsetting` `err_busy` after that (curmenu still ok). recmode/playmode/capture while running answer `err_busy` (assumption, not seen on the body). |
| GENA | `SUBSCRIBE` (CMS or CDS event URL) -> `200`, `SID: uuid:4D454931-000N-1000-8000-<MAC>`, `TIMEOUT: Second-300`; renew by SID -> 200, unknown SID -> 412; `UNSUBSCRIBE` known -> 200, unknown -> 412. NOTIFY callbacks are real HTTP `NOTIFY` requests with the recorded headers (`NT`, `NTS: upnp:propchange`, `SID`, per-subscription `SEQ`) and body format; SEQ 0 is the recorded ConnectionManager propertyset ~70 ms after SUBSCRIBE. |
| Post-SUBSCRIBE switch | Every fresh SUBSCRIBE opens a settle window of 5-22 s (`--settle`). In rec the camera silently goes to play within 0.8 s; at the end of the window it sends `mod_Play`. Before the first SUBSCRIBE the camera is on "Connecting": `startstream` answers ok but sends 0 datagrams. |
| Live view | `startstream&value=<port>` -> UDP to the requesting IP, ~25 fps, 22.7-31 KB datagrams: the recorded 254-byte header (bytes 0-1 BE16 length, 9-14 MAC, 15-16 `0x0021`, 30-31 `0x00de`) + a generated 640x360 baseline JPEG (moving bar, clock, per-camera hue); 320x184 / 12.8-13.6 KB when `liveviewsize` was `qvga` at `startstream` (`hd` and other values answer err_param). Stops when the camera goes to play, on `stopstream`, or after 10 s without any request from the viewer. |
| Browse | `POST /Server0/CDS_control` Browse (`BrowseDirectChildren` and `BrowseMetadata`), DIDL-Lite in the recorded S5 shape (titles `100-0001`, `DO/DT/DL` URLs, fixed fake `size` 5000/100000 on DT/DL, `object.container` + `pana:X_RecGroupType Interval` groups), paged by `StartingIndex/RequestedCount` (max 100 per page). UPnP fault 701 in rec mode and for 0.6 s after switching to play. |
| Card | 4 photos (one RAW+JPEG), a 6 s MP4, a 2 s MOV without `dc:date` (like the S5), a 120-shot time-lapse group, one more photo; captures/videos/time lapses append. `get_content_info` counts it (`err_busy` while wedged, `err_reject` outside play). The card and the menu settings (curmenu + getsetting) survive power cycles and `/emu/reset`; only a new emulator process starts from the recorded state. |
| `:files/<name>` | GET/HEAD for `DO<id>.JPG/.RW2/.MP4/.MOV`, `DT<id>.JPG` (160x120), `DL<id>.JPG`; real JPEG headers + deterministic filler up to the advertised size; `Range` (206, suffix ranges, 416); `X-CONVERT: SmallSize|MediumSize` returns the thumb/preview. 404 outside play mode. |

### Wedge realism (on by default)

| Trigger | Result |
|---|---|
| any camcmd or `stopstream` during the post-SUBSCRIBE settle window | `err_critical` and the camera is **wedged**. For `recmode` the queued NOTIFYs `lens_Atta`, `mod_Play`, `mod_Rec` follow and it ends in rec (FINDINGS 21:24 A/B, 22:45). |
| `recmode` churn: the 4th recmode within 20 s | `err_critical`, the camera switches itself to play (`mod_Play`), wedged (FINDINGS 22:01). |
| `stopstream` with no stream started | `err_critical` (no wedge outside the settle window). |
| Photo fault, off by default (`EmuConfig.photo_fault_*`; real S5 2026-10-12 16:46-16:52, `tests/fixtures/real-s5-photo-fault-wedge-2026-10-12.log`) | After N good captures the body drops to play on its own 13.2 s later (`mod_Play`, nothing sent). Every later capture: ok, `lens_Atta`, then `X_Panasonic_Cam_PRec=err_rec` + play + `mod_Play` 3.7 s later and no photo. `recmode` -> ok and rec; the 1st/4th/7th... bounce back to play 1.3 s later and return to rec 3 s after that. `startstream` within 2 s of a mode change -> `err_busy`. After 4 recmodes in the fault within 300 s the next capture hard-wedges it 6.6 s later: getstate/getsetting `err_busy`, camcmd/stopstream/`req_acc` `err_critical` (real: stopstream got no reply) until a power cycle. |
| card write after `capture` (1.2 s) / `video_recstop` (2 s), body busy (`/emu/body/busy`) | camcmd -> `err_busy`; `sd_access on`. |
| `poweroff` in play | `err_reject` (as logged); in rec -> ok, ports close 0.5 s later. |
| session dropped | every command -> `err_reject`. |

While wedged: every valid camcmd -> `err_busy`, unknown camcmd -> `err_param`, `startstream` alternates ok/`err_busy` and never sends a frame, `get_content_info` -> `err_busy`; setsetting/getsetting/getinfo/camctrl keep working, and Browse/:50001 still work if the camera sits in play. Only `/emu/power-cycle` (or `/emu/reset`, `/emu/unwedge`) clears it. With `--wedge-realism off` none of the above happens (stopstream without a stream is ok, no busy windows) but the mod_Play-after-SUBSCRIBE event timing stays.

## Control API (`--control-port`, default http-port + 1)

| Endpoint | Effect |
|---|---|
| `GET /emu/state` | JSON: mode, wedge (+reason), session(s), subscriptions, stream (target, frames sent), interval, card counts, settings, config, counters, last events. |
| `GET /emu/log?n=200&kind=cgi\|upnp\|dl\|notify\|wedge\|session\|power\|body` | Every request/NOTIFY with time, client IP, result, cammode and wedged flag. Use it in QA to assert the client never sent e.g. recmode before mod_Play. |
| `POST /emu/reset` | Fresh boot (play, no session, not wedged), ports stay open; card and settings are kept. getstate still answers before a new req_acc, so this is the "camera restarted between two keepalives" case. |
| `POST /emu/power-cycle?off_s=1` | Ports closed (connection refused) for `off_s`, then a fresh boot. |
| `POST /emu/power-off`, `/emu/power-on` | Separately. |
| `POST /emu/wedge?reason=...`, `/emu/unwedge` | Force / clear the wedge. |
| `POST /emu/drop-session[?ip=...]` | Camera drops the session (`err_reject` until req_acc). |
| `POST /emu/approve` | Approve a pending `--approval prompt` request. |
| `POST /emu/body/{play,rec,busy,update,lens_detach,lens_attach,self_play}` | Someone uses the body: play -> `mod_Play`; busy -> `busy` (camcmd err_busy, live view stops); rec -> `update` (if busy), `lens_Atta`, `mod_Rec`; self_play -> play + `mod_Play`, back to rec + `mod_Rec` by itself `fault_bounce_back_s` (3 s) later (real 16:46:58-16:47:01). |
| `POST /emu/config` (JSON body or query) | Change any `EmuConfig` field live, e.g. `{"settle_min": 1, "settle_max": 2, "session_timeout_s": 0}`. |

## Fidelity gaps (not observed on the real S5, so guessed or missing)

- **No SSDP** (`ssdp:alive`/`byebye` on boot, M-SEARCH is ignored by the S5 anyway), no PTP/IP on 15740, no BLE.
- Session idle timeout (30 s), live-view keepalive (10 s), card-write busy durations, max Browse page (100), and the 0.6 s Browse 701 window after playmode are guesses.
- `playmode` sends `mod_Play` (plausible, not recorded); `recmode` while already in rec with a running stream -> `err_busy`, without a stream -> ok + echo events (both were seen at different times).
- The real camera sometimes answered `err_reject` for a few seconds and then recovered without a new req_acc (server log 20:57:11, 21:09:15). Not modelled; use `/emu/drop-session` + reconnect.
- `startstream` in play -> `err_reject`, `get_content_info` in rec -> `err_reject`, :50001 in rec -> 404: guesses.
- poweroff in rec mode was never tried on the real body.
- No `X_Panasonic_Cam_VRec` / `Cam_PictBrst` / `Cam_Transfer` events (`Cam_PRec` only in the photo fault); no `busy`/`update` around captures (only via `/emu/body/*`).
- getstate `interval_status` values (`rec`/`pause`/`off`/`enable`) and `interval set` -> `err_reject` once armed are from the real S5 (2026-10-12); `playmode`/`recmode` into a *running* (not paused) run is not verified.
- The RAW `<res>` protocolInfo (`CAM_RAW_JPG`) is a guess; RW2/MP4/MOV bodies are a valid magic/box header plus filler, not playable media. JPEGs are real but blocky (8x8 flat blocks, no AC coefficients).
- Live-view header bytes other than length/MAC are the recorded frame 1 for every frame (the real header carries changing exposure data).
- Only one model profile (`dc-s5`). Touch type/allmenu/capability are the S5's.
- Live view is only sent while the session is accepted; the real camera may behave differently with several clients.

## Tests

`tests/test_timelapse_emu.py` drives the real server against the emulator (timelapse engines, quiesce). `tests/test_emulator.py` (unittest, no camera): handshake + ddd scrubbing, GENA subscribe/NOTIFY/renew/412, session drop and idle timeout, the three wedge triggers, busy windows, touch, poweroff/power-on, body events, realism off, live-view datagram layout/fps/stop rules, "Connecting" (no frames before SUBSCRIBE), Browse 701 + paging of a 120-item group, Range downloads, time lapse, two independent cameras.

```sh
.venv/bin/python -m unittest tests.test_emulator
```
