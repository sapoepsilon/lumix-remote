# QA matrix (emulator pass, 2026-10-12)

Every user-facing feature, run end to end against the camera emulator
(`docs/emulator.md`): two emulated DC-S5 bodies, the real server
(`./run.sh`), the web UI in headless Chromium (Playwright, phone 390x844 and
320x640 touch, landscape phone 844x390, desktop 1440x900), every CLI command,
and the HTTP API with curl. Failures were injected with the emulator's control
API (`/emu/wedge`, `/emu/drop-session`, `/emu/reset`, `/emu/power-cycle`,
`/emu/body/busy|play|rec`, `dl_rate` throttle, realistic 5-22 s settle).

After every scenario the emulator's request log was checked for protocol rule
violations (`camcmd`/`stopstream` before the post-SUBSCRIBE `mod_Play`,
`stopstream` with no stream = `err_critical`, more than 3 `recmode` in 20 s,
any wedge, camcmds sent to a wedged camera). The only wedges in the final run
are the ones injected on purpose (rows E5, X5).

Setup (isolated from the real camera and the user's files):

```sh
# emulators: cam.cgi 18080/18180, upnp 18606/18706, files 18001/18101, control 18081/18181
.venv/bin/python -m emulator --http-port 18080 --upnp-port 18606 --dl-port 18001 \
  --control-port 18081 --count 2 --port-stride 100 --settle 3-5 --interval-scale 0.05
LUMIX_REMOTE_HOME=/tmp/lumix-qa/home LUMIX_IMPORT_DEST=/tmp/lumix-qa/pictures \
  ./run.sh --port 18770 --event-port 49253 --lv-port 49299 --no-scan --log-dir /tmp/lumix-qa/logs
```

Evidence: screenshots `evidence/qa-*.png`, scenario transcripts and per-run
rule checks `evidence/qa-logs/*.out|*.rules1|*.rules2` (emulator 1 / 2),
CLI transcript `evidence/qa-logs/cli.out`, smoke-script run
`evidence/qa-logs/smoke.out` (the unit tests were removed on 2026-10-15)
(`evidence/` is not committed).

Result: **98 rows: 97 pass, 0 fail, 1 not run (B3, real Bluetooth)**. 33 of
the passes failed in the first round and were fixed and re-verified ("fixed",
commit named). Four further known limitations are listed at the end.

## Onboarding and cameras

| # | Feature | Steps | Expected | Result | Evidence |
|---|---|---|---|---|---|
| O1 | First run, no camera found | fresh `LUMIX_REMOTE_HOME`, `--no-scan`, open `/` (phone) | connect screen "Looking for your camera", S5 menu-path checklist | PASS | qa-01, s01_onboard.out |
| O2 | Wizard: model step | Set up step by step | Detect automatically (default), Lumix S5 "tested", Other | PASS | qa-02 |
| O3 | Wizard: Wi-Fi step + live tracker | Continue; camera appears | steps for the S5; Looking -> Approve -> Connected; Continue enables | PASS | qa-03..05 |
| O4 | Wizard: connect-settle wording | watch stage text | plain language | fixed (7a26bbf: "finishing the connection (the camera needs 5-30 s)") | s01_onboard.out |
| O5 | Wizard: Bluetooth step | Skip | pairing explained, Mac-leaves-Wi-Fi warning; skippable | PASS (pairing not run, see B3) | qa-06 |
| O6 | Wizard: name + done | type "Studio S5", Save, Start shooting | summary (model, Wi-Fi, BLE, id); main view with live view and stats | PASS | qa-07..09 |
| O7 | Second camera via wizard | Controls > Cameras > Add camera..., connect, name "B-cam" | new entry, connects, Start shooting switches to it | PASS | qa-11..15, s02_second.out |
| O8 | Camera switcher | tap camera name (2 cameras), pick other | sheet lists both with status; switches live view, controls, active camera | PASS | qa-16, qa-17 |
| O9 | Camera details | (i): rename, model, Connect automatically, BLE watchdog, remove | each saved (toast), autoconnect off -> "paused", watchdog flag on, remove dialog | PASS | qa-18, qa-A0..A2, s13_misc.out |
| O10 | No camera at all | remove every camera | wizard opens; after Cancel: "No camera yet" + "Add a camera" (no Reconnect / address form) | fixed (f16aa37) | qa-95, qa-96 |
| O11 | No camera: shutter / Files | press shutter, open Files | "No camera set up yet. Add one first." / "No camera yet" (was "no camera set up" / "server doesn't have the files feature") | fixed (f16aa37) | qa-97 |
| O12 | Wizard Back / Esc / deep links | `#wizard=name`, Back, Esc; `#cameras` | steps back; Esc closes and keeps the existing camera; `#cameras` opens the list | PASS | s13_misc.out |

## Shooting

| # | Feature | Steps | Expected | Result | Evidence |
|---|---|---|---|---|---|
| S1 | Live view | open main view | MJPEG frames ~25 fps, stats (battery, shots, card) | PASS | qa-09, qa-10 |
| S2 | Photo (button, Space) | shutter, Space | "Photo saved · N left", count drops, flash | PASS | qa-20, s03_shoot.out |
| S3 | Video (VIDEO mode, R) | 2, shutter, R | recording badge + timer; stops; MP4 on card | PASS | qa-21 |
| S4 | Mode strip | 1/2/3, arrows, swipe/tap | modes switch, wraps at ends | PASS | s03_shoot.out |
| S5 | One-shot AF | AF button, F | `oneshot_af -> ok` | PASS | s03_shoot.out |
| S6 | Tap in MF = Ignore | MF, tap live view | toast, no touch sent | PASS | qa-22 |
| S7 | Tap in MF = Magnify | Controls > Tap in MF > Magnify, tap | `camctrl touch on` + release | PASS | s04_controls.out |
| S8 | Close MF Assist inset | Controls > Close MF Assist inset | focusmode afs then back to mf, "Inset closed" | PASS | s13_misc.out |
| S9 | Quick controls | ISO 400, focus AF-S, WB, aperture, shutter | tile shows the new value, camera has it | fixed (a81916a: emulator never updated curmenu for ISO; UI showed AUTO) | qa-31, qa-32 |
| S10 | Auto on | Controls > Auto switch | saved manual values, iA + AF + auto ISO/WB/EV0, readout "AUTO" | PASS | qa-33 |
| S11 | Auto retune photo -> video | VIDEO with Auto on | "Auto tuned for video", AF-C | PASS | s04_controls.out |
| S12 | Auto off restores | AUTO button | every saved value back (ISO, WB, focus, mode, drive, AF area, EV) | fixed (a81916a, 900f1bd: emulator getsetting/curmenu disagreed; Time Lapse sub-choice lost) | s04_controls.out "RESTORED OK" |
| S13 | Auto refused by the camera | Auto on right after the camera rebooted | 409 + reason, Auto stays off, saved values kept | fixed (c7bb1ff) | test_qa_regressions AutoRefusedTest |
| S14 | All settings panel | Controls > All settings, search "peak", pick option, restore | 53 settings in sections, filter, options with tick, toast "Focus Peaking: Off" | PASS | qa-40..42 |
| S15 | Esc in settings search | type, Esc x3 | clears, leaves the field, closes the sheet | fixed (366db0c: Esc was swallowed by the field) | s06_files.out |
| S16 | Shutter while camera in playback (body) | `/emu/body/play`, shutter | switches back to rec, photo ok | fixed (5d0ab29: was err_reject) | ShutterFromPlaybackTest |
| S17 | Readout / exposure | change aperture, shutter | readout F/shutter/ISO/EV/focus | PASS | qa-32 |

## Files

| # | Feature | Steps | Expected | Result | Evidence |
|---|---|---|---|---|---|
| F1 | Browse | G / photo button | grid of 60, count 127+, thumbs load, camera in play | PASS | qa-50 |
| F2 | Filter | Photos / Videos / All | 2 videos (MP4 + MOV), photos only | PASS | qa-51 |
| F3 | Preview | tap a photo / video | large preview, meta (date, size), video note | PASS | qa-52, qa-53 |
| F4 | Download one | viewer download | JPEG saved with the card name | PASS | s06_files.out |
| F5 | Select + multi-download | Select, 3 tiles, download | 3 downloads | PASS | qa-54, s06_files.out |
| F6 | Import (with RAW) | Import to Mac | dialog shows the real default folder, progress, "Imported 3 files in ..." | fixed (366db0c: placeholder said "server decides") | qa-55..57 |
| F7 | Import again | same file | "Already on this Mac: 1 file" (was "Imported 0 of 1") | fixed (366db0c) | s06_files.out |
| F8 | Back to shooting | Camera (back) | `files/done`, rec mode, live view < 1 s | PASS | qa-58 |
| F9 | Import paused by leaving, resumed | throttled import, leave Files, reopen | job paused (`.part` kept), resumes with Range, progress shown again | fixed (366db0c: resumed job had no progress UI) | qa-60..62, s07_import_pause.out |
| F10 | Shooting while browsing (CLI) | `lumix files list`, `lumix shoot` | leaves playback and shoots (was err_reject); 409 with reason while an import copies | fixed (ad24ea9) | ShootingLeavesFilesTest |
| F11 | Files bar at 320 px | select at 320x640 | bar fits ("Import") | fixed (40b6d0a: Import button ran off-screen) | qa-L-phone320-files-select |

## Timelapse

| # | Feature | Steps | Expected | Result | Evidence |
|---|---|---|---|---|---|
| T1 | Setup panel | TIMELAPSE mode | engine, interval, shots/time, options, summary with the real import folder | fixed (366db0c: summary hard-coded ~/Pictures/Lumix) | qa-70 |
| T2 | Server engine run | Mac, 2 s, 30 shots, Import, shutter | ring, shots, ETA, next shot, last frame | PASS | qa-71 |
| T3 | Pause / resume | Pause, wait, Resume | no shots while paused, grid re-anchored | PASS | qa-72 |
| T4 | Reconnect mid-run | `/emu/power-cycle` | waits, counts missed slots, continues | PASS | qa-73, s08_timelapse.out |
| T5 | Stop via shutter | shutter | stopped, frames imported, "Last run stopped: 9 shots" | PASS | qa-74 |
| T6 | Native engine | In-camera, 10 s, 30 shots | `interval set`, run, pause/restart, stop, drive mode restored | PASS | qa-75, qa-76, qa-79, s09_native.out |
| T7 | Native: shutter / Files refused | PHOTO + Space, G | 409 with "in-camera interval run in progress", not a wedge | PASS | qa-77, qa-78 |
| T8 | Later (scheduled) | Later, shutter | "Starts soon · starts at HH:MM"; Stop returns to setup | fixed (6582d4b: panel stuck on "Stopping" when the HTTP answer lost the race with the push) | qa-A3 |
| T9 | Busy window during a run | `/emu/body/busy` 6 s | 0 camcmds sent, slots skipped with reason | PASS | stress.out (camcmds during busy: 0) |
| T10 | Session drop during a run | `/emu/drop-session` | reconnect, missed slots counted, run continues | PASS | stress.out |

## Power, quiesce, connection

| # | Feature | Steps | Expected | Result | Evidence |
|---|---|---|---|---|---|
| P1 | Power-off sheet, idle | Controls > Power off camera... | "Nothing running. Safe to power off." | fixed (366db0c: listed live view / event subscription as if they were work) | qa-80 |
| P2 | Power-off sheet, busy | timelapse + recording running | stops recording (waits for card), pauses timelapse, resume note | PASS | qa-81 |
| P3 | Power off + quiesce | Power Off | recording written (MP4 on card), timelapse paused, camera off | PASS | qa-82, s10_power.out |
| P4 | Status while off | wait | "Camera is off" (was "dropped off" + raw ClientConnectorError after the first retry) | fixed (bb12f91, f16aa37) | qa-82 |
| P5 | Auto-resume | `/emu/power-on` | reconnect, timelapse resumes ("camera back: resumed timelapse") | PASS | qa-83 |
| P6 | Connection page | Controls > Connection, Reconnect | details, reconnect ok | PASS | qa-90 |
| P7 | Session dropped by camera | `/emu/drop-session` | banner, reconnect by itself | PASS | qa-91 |
| P8 | Camera restarted between keepalives | `/emu/reset` (getstate still answers) | noticed within 15 s idle / 1.5 s on a command, reconnects | fixed (4e31748: stayed "ready" forever, every command err_reject) | RebootDetectionTest |
| P9 | Connect to an address | connect screen > address 192.0.2.1, then 127.0.0.1 | tries the address, comes back | PASS | qa-B1, s14_connect.out |
| P10 | Forget remembered camera | connect screen > Forget | searches from scratch; setting the IP again reconnects | fixed (6c9ec98: fixed IP stayed in cameras.json, re-setting it did nothing) | qa-B2, ForgetAndAddressTest |
| P11 | Realistic settle (5-22 s) | reconnect with `settle_min 5, settle_max 22` | waits for mod_Play, no camcmd before it | PASS | s14_connect rules |
| P12 | Switch active camera, then restart a session | switch to B-cam, forget | session keeps its own live-view port | fixed (3244f8e: took camera 1's `--lv-port`, "Address already in use") | test_overrides_stay_on_the_startup_camera |
| P13 | Event log | Controls > Event log | newest first, kinds | PASS | qa-B3 |

## Error handling

| # | Feature | Steps | Expected | Result | Evidence |
|---|---|---|---|---|---|
| E1 | Body in use (`busy`) | `/emu/body/busy`, shutter | 409 "Camera is being operated on the body", banner, no camcmds | fixed (49bb0a9, f16aa37: toast said "busy with another command", no banner) | qa-92 |
| E2 | err_busy during card write | double shutter | server retries, second photo ok | PASS | s11_errors.out |
| E3 | Wedged camera | `/emu/wedge`, shutter, R | flagged after one exhausted budget; red dot, "Camera stuck: switch it off and on again", no Reconnect; later commands refused locally | fixed (49bb0a9, f16aa37 + red dot / live view note: banner said "Camera busy: check its screen", dot stayed green) | qa-93 |
| E4 | No spam while wedged | 20 s idle | 0 camcmds to the wedged camera | PASS | s11_errors.out |
| E5 | Power cycle clears wedge | `/emu/power-cycle` | wedged false, live view back | PASS | s11_errors.out |
| E6 | recmode churn guard | CLI: `cmd recmode`, `files list`, `files done`, `shoot` in quick succession | never more than 3 recmode in 20 s; a 4th waits for a slot; recmode in rec isn't sent | fixed (45bd9cb, 5d0ab29: up to 5 recmode in 5 s at connect; camera left in playback) | test_camera guard tests, cli.out |
| E7 | stopstream discipline | server stop, session drop in playback | no stopstream without our stream | fixed (4e31748: err_critical on every stop/disconnect) | StopstreamDisciplineTest |
| E8 | Server not reachable | stop server with a tab open | "Server not reachable" banner, reconnects when back | fixed (c69bae6) | qa-98, qa-99 |

## Server and PWA

| # | Feature | Steps | Expected | Result | Evidence |
|---|---|---|---|---|---|
| V1 | `lumix server stop` with a browser open | open UI, stop | stops in < 1 s, clean shutdown (was 10 s + SIGKILL, cameras never stopped) | fixed (c69bae6) | ShutdownClosesStreamsTest |
| V2 | Pidfile | start/stop/status | pidfile written, removed on stop, `status` reports pid | PASS | cli.out |
| V3 | Takeover | second `./run.sh` | "stopping previous server", only one runs | PASS | server.out |
| V4 | Quiesce on server stop | stop with a timelapse running | paused (server-stop), resumes after start | PASS | lumix-server.log |
| V5 | PWA manifest | GET /manifest.webmanifest + icons | `application/manifest+json`, standalone, 192/512/maskable PNGs at the declared sizes | PASS | pwa.txt |
| V6 | Stale app.js after an update | index.html | asset URLs versioned (`?v=<mtime>`) | fixed (11c2b36) | IndexAssetVersionTest |
| V7 | Layout: phone 320/390, landscape, desktop | every view | no horizontal overflow, controls reachable; closed sheet inert | fixed (40b6d0a) | qa-L-* (28 screenshots) |
| V8 | Keyboard shortcuts page | Controls > Keyboard shortcuts | list matches the handlers; tooltips Files = G, AF = F | fixed (366db0c: Files tooltip said F) | qa-L-*-keys |
| V9 | Paused camera status | camera with autoconnect off | `status: paused` + why (was "idle" forever) | fixed (5c58c5d) | PausedStatusTest |

## CLI (every command, `evidence/qa-logs/cli.out`)

| # | Command(s) | Result |
|---|---|---|
| C1 | `status`, `info`, `state`, `get`, `get iso`, `get --raw focal` | PASS; status of a never-connected camera and in playback fixed (25c72a5, 76c758b) |
| C2 | `settings list [-f] [--brief] [--names]`, `settings options X [--names]` | PASS |
| C3 | `set iso 400`, `set exposure3 -1/3`, `set whitebalance daylight`, `set aperture 5.6`, `set shutter 250`, `set --raw focal 1024/256`; bad value / unknown setting exit 2 | PASS |
| C4 | `shoot`, `video start/stop/toggle`, `af`, `focus 50% 50%` / `0.2 0.8` / `900 100`, `zoom tele --fast|stop|wide` | PASS; `video stop` when not recording no longer sends `video_recstop` (5d0ab29) |
| C5 | `cmd recmode`, `cmd poweroff` (refused, exit 1) | PASS; recmode in rec is not sent (5d0ab29) |
| C6 | `snapshot -o` | PASS (640x360 JPEG) |
| C7 | `events -n`, `events -f --all --count`, `events -f --json` | fixed (e2f17e0: camera-list pushes were dumped as raw JSON) |
| C8 | `poweroff` (plan, exit 2), `poweroff --json`; `poweroff --yes` via UI P3 | PASS |
| C9 | `timelapse start/status/pause/resume/stop`, `--start-at +3600`, `--native`, `status --watch`, missing count (exit 2), second start (409) | PASS |
| C10 | `files list/download/import/status/done/jobs/cancel/pause/resume` | was a placeholder; implemented (a093520) and PASS |
| C11 | `cameras list/add/use/rename/remove`, `--camera`/`-c`, `LUMIX_CAMERA`, unknown camera | PASS; `cameras list` without a server reads cameras.json |
| C12 | `server start/stop/status` | PASS |
| C13 | `completion bash|zsh` (fish refused, exit 2) | PASS |

## BLE (status only)

| # | Feature | Result |
|---|---|---|
| B1 | `GET /api/ble/status`, per camera | PASS (registered false, watchdog state) |
| B2 | Watchdog switch (UI and `POST /api/ble/watchdog`), bad body -> 400 | PASS |
| B3 | Pairing, wake, power-cycle | LIMIT: not run (real Bluetooth; out of scope for this pass) |

## Real-camera smoke script

| # | Feature | Result |
|---|---|---|
| R1 | `scripts/real-camera-smoke.sh` against the emulator (same steps as on the S5) | PASS, 8 steps, drive mode restored (smoke.out) |
| R2 | Abort on a wedged camera | PASS: stops at step 3 with the power-cycle instruction, nothing more sent |

## Known limitations

- **LIMIT B3**: Bluetooth pairing / wake / power-cycle were not exercised (they
  drive the real radio and the Mac's Wi-Fi).
- **LIMIT L1**: the add-camera wizard can't set camera-side ports, so an
  emulator needs `lumix cameras add --http-port ...` or `PATCH /api/cameras/{id}`
  (real bodies use the default ports; this QA set them through the API at the
  "camera appears" moment).
- **LIMIT L2**: "Connect to a specific address" on the connect screen can close
  itself when the camera was ready a moment before (the screen hides on
  "ready"); the request still goes through.
- **LIMIT L3**: emulator fidelity: everything here ran against the emulator's
  model of the S5 (see `docs/emulator.md` "Fidelity gaps"); the real body is
  checked by `scripts/real-camera-smoke.sh`. The recmode guard (max 3 in 20 s)
  and the session probe (`getsetting touch_type` after an unexpected
  `err_reject`, every 15 s without frames) are new behaviour on the real body.
- **LIMIT L4**: PWA install on a real iPhone and `--lan` access from a phone
  were not tested (headless Chromium only); multi-file download on iOS Safari
  may open files one by one.
