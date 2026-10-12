# Lumix Remote

Control a Panasonic Lumix camera over Wi-Fi from a browser: live view, shutter,
video, touch focus, exposure controls and every setting the camera exposes
remotely. Built and tested against a **DC-S5 (firmware 2.03)**; other Lumix
bodies that speak the same `cam.cgi` protocol (LUMIX Sync / Image App) may work.

The protocol is reverse-engineered (see `docs/protocol-research.md`). Panasonic
publishes no spec, so this is unofficial and may break with firmware updates.

## Quick start

```sh
./run.sh                       # http://127.0.0.1:8770, camera auto-discovered
./run.sh --camera-ip 192.168.1.50
./run.sh --lan                 # also reachable from your phone on the LAN (no auth!)
```

`run.sh` creates `.venv` and installs `requirements.txt` (only `aiohttp`) on first
run. Python 3.9+.

On the camera: connect it to the same Wi-Fi network as your computer
(Wi-Fi > "Remote Shooting & View", via network) or join its own access point
(camera at 192.168.54.1). The first connection registers this computer on the
camera; if it asks, approve it there. The camera then shows "Under remote control".

Options: `--port`, `--host`, `--device-name` (name shown on the camera),
`--lv-port` (UDP live view, default 49199), `--event-port` (HTTP callback for
camera events, default 49153), `--scan-exclude ip1,ip2` (hosts the subnet scan
must never contact; also `LUMIX_SCAN_EXCLUDE`), `--log-dir` (raw request log,
default `logs/`), `-v`.

Your firewall must allow inbound UDP on the live view port and TCP on the event
port from the camera.

### Discovery

Order: `--camera-ip` > last working IP (`~/.config/lumix-remote/state.json`) >
SSDP announcement heard since start > camera AP address 192.168.54.1 > TCP scan
of the local /24 for port 60606. The S5 ignores SSDP M-SEARCH and only
announces itself when it joins the network, so the scan is the usual fallback.

### Power

"Power off" sends `camcmd poweroff` after a confirm dialog. The camera can't
be switched back on over Wi-Fi; see "Bluetooth" below for waking it over BLE.

## Web UI

Styled after the iPhone Camera app: dark, full-bleed live view, translucent
controls, works on a desktop browser and at phone width. Add it to the iPhone
home screen (Share > Add to Home Screen; needs `--lan`) and it opens full screen.

- **Connect screen** on first run: searching / connecting / approve on camera /
  reconnecting, with the camera-side steps as a tappable checklist, a Reconnect
  button, "connect to address" and "forget camera". The last camera is
  remembered (server: `state.json`, browser: localStorage). After a drop the
  server reconnects on its own; the UI shows a small banner meanwhile, and a
  "camera busy" banner when live view stalls after `err_busy` answers.
- **Shooting view:** big round shutter, PHOTO / VIDEO / TIMELAPSE strip (swipe
  it or the live view, or use the arrow keys), exposure readout, one-shot AF,
  tap to focus. Timelapse is a placeholder for now.
- **AUTO** button: one tap applies the Auto preset (below); tap again to get
  your own settings back.
- **Controls sheet** (sliders button or `P`): Auto switch, quick controls
  (aperture, shutter, ISO, EV, WB, mode, focus, AF area, drive, photo quality
  and size, video quality), focus helpers, and **All settings**, generated from
  the camera's own menu (every setting the body allows remotely), searchable.
  Also connection details, event log, shortcuts, power off.
- **Files** (photo button or `G`): iOS-Photos-style grid, filter, tap to preview,
  Select for multi-select, Download, Import to Mac with progress.

Keyboard: Space photo, R video, 1/2/3 or arrows mode, P controls, A Auto,
F one-shot AF, G files, Esc closes.

### Auto preset and MF Assist (DC-S5 findings)

The S5 lets the exposure mode be set remotely (`setsetting recmode ia`) even
though it has a mode dial; the dial wins again when it's turned. Auto applies:
Intelligent Auto, single-shot drive (a Time Lapse drive mode turns the shutter
into "start time lapse"), AF-S for photo / AF-C for video, AF area
auto-detection (face/eye/body), ISO auto, AWB, EV 0, multi metering. Picture
and video quality are left alone. If a body rejects iA, it falls back to
Program AE (photo) or Creative Video P (video).

Before the first apply the current values are read and saved in
`~/.config/lumix-remote/presets.json` (`getsetting`, except the rec mode which
the S5 only reports through `curmenu`); Auto off writes them back, rec mode
first. Switching PHOTO/VIDEO while Auto is on re-tunes it without losing the
saved values.

The magnified picture-in-picture inset in the live view is the camera's
**MF Assist**: with focus on MF, a touch on the screen opens it. The S5 does not
expose MF Assist / its PIP display style in `allmenu`, so it can't be switched
off remotely. The UI therefore ignores taps while focus is MF (Controls > Tap in
MF), Auto uses AF, and "Close MF Assist inset" flips focus to AF-S and back.

## CLI

`bin/lumix` drives the camera from a terminal or script. It only talks to the
server's HTTP/WebSocket API (the camera accepts a single controller, and the
server holds that session), so the server must be running. It uses the repo's
`.venv` (no extra dependencies); put it on your PATH with
`ln -s "$PWD/bin/lumix" ~/.local/bin/lumix`.

Server address: `--server URL` or `LUMIX_SERVER`, default `127.0.0.1:8770`.
Every read command takes `--json`. Exit codes: 0 ok, 1 camera or server error
(the camera's raw answer such as `err_busy` goes to stderr), 2 usage error or
unknown setting/value, 3 server unreachable.

```sh
lumix server start               # background ./run.sh, pid + log in logs/lumix-server.{pid,log}
lumix server start -- --camera-ip 192.168.1.50
lumix server status              # also: lumix server stop (only stops a server it started)

lumix status                     # battery, cards, mode, remaining shots/video, live view
lumix status --json | jq .remain_photos
lumix info                       # device, lens, capabilities, aperture/shutter
lumix state                      # raw getstate fields

lumix get                        # quick controls: aperture, shutter, mode, ISO, WB, ...
lumix get iso                    # one setting: current value + allowed values
lumix get --raw focal            # raw camera getsetting
lumix settings list              # all 50+ menu settings, current + allowed values
lumix settings list -f bracket --brief
lumix settings options whitebalance   # '*' marks the current one

lumix set iso 400                # by command type, short name (sensitivity) or title
lumix set exposure3 -1/3         # value, title or token from `settings options`
lumix set whitebalance daylight
lumix set aperture 5.6           # also F5.6 or 1440/256
lumix set shutter 250            # 1/250; 1s or 2" for slow speeds
lumix set --raw focal 1024/256   # raw setsetting TYPE VALUE [VALUE2]

lumix shoot
lumix video start|stop|toggle
lumix af                         # one-shot AF
lumix focus 50% 50%              # touch AF; 0..1000, 0.0..1.0 or percent
lumix zoom tele --fast; lumix zoom stop
lumix cmd recmode                # raw camcmd passthrough
lumix snapshot -o frame.jpg      # latest live view frame ('-o -' = stdout)
lumix events -n 20               # recent event log
lumix events -f                  # follow the WebSocket feed (--json, --all, --count N)
lumix poweroff --yes             # cannot be switched back on over Wi-Fi
```

Setting names and option tokens are shell-safe for completion
(`lumix settings list --names`, `lumix settings options iso --names`). Shell
completion: `eval "$(lumix completion bash)"`, or for zsh
`autoload -U +X bashcompinit && bashcompinit && eval "$(lumix completion zsh)"`.

`lumix timelapse` and `lumix files` are placeholders until the server grows
those features.

### Tests

```sh
.venv/bin/python -m unittest discover -s tests
```

`tests/test_cli.py` runs every CLI command against a fake server (aiohttp in a
background thread), including the WebSocket feed and `server start/stop` with a
stand-in `run.sh`; no camera needed.

## HTTP API

Everything the UI does goes through this JSON API, so scripts and a CLI can use
it while the server holds the camera's single controller session.
POST bodies are JSON. Camera answers come back as `result` (`ok`, `err_busy`,
`err_param`, `err_reject`, `err_critical`, ...). HTTP 409 = camera busy with
another operation or not connected, 400 = bad request, 502 = transport error.

| Method & path | What |
|---|---|
| `GET /api/status` | Connection status, camera, battery, card, recording, live view stats |
| `GET /api/state` | Raw `getstate` fields |
| `GET /api/info` | Device description, capabilities, lens, touch type, aperture/shutter options |
| `GET /api/events` | Recent event log (commands, camera NOTIFY events, state changes) |
| `GET /api/settings` | Every menu setting: id, title, section, path, current value, options (id/title/value/enabled) |
| `GET /api/settings/{id}` | One setting |
| `POST /api/settings/{id}` `{"option": "<option id>"}` | Select an option of a menu setting |
| `GET /api/quick` | Quick-control subset + aperture/shutter (current + options) |
| `POST /api/setting` `{"type","value","value2"?}` | Raw `setsetting`, e.g. `{"type":"focal","value":"1024/256"}` |
| `GET /api/getsetting/{type}` | Raw `getsetting`, e.g. `focal`, `shtrspeed`, `iso`, `touch_type` |
| `POST /api/capture` | Take a photo (returns remaining-shots before/after) |
| `POST /api/video/start`, `POST /api/video/stop` | Video recording |
| `POST /api/touch` `{"x","y"}` | Touch AF at 0..1000 live-view coordinates |
| `POST /api/af` | One-shot AF |
| `POST /api/zoom` `{"action"}` | Power zoom: `tele-normal`, `tele-fast`, `wide-normal`, `wide-fast`, `zoomstop` |
| `POST /api/camcmd` `{"value"}` | Any `camcmd` (e.g. `recmode`, `playmode`, `oneshot_af`); not `poweroff` |
| `POST /api/power/off` `{"confirm": true}` | Power the camera off |
| `GET /api/liveview.jpg` | Latest live view frame |
| `GET /api/liveview.mjpg` | MJPEG stream (`multipart/x-mixed-replace`) |
| `GET /api/ws` | WebSocket push: `status`, `exposure`, `settings` (changed ids), `event`, `events`, `preset` |
| `GET /api/presets` | Preset state: `{"auto": {on, target, saved, applied, recmode_used, since}}` |
| `POST /api/presets/auto` `{"on": true, "target": "photo"\|"video"}` | Auto on (saves manual values first) / off (`{"on": false}` restores them). Returns `steps` (each setsetting + result), `before`, `after` |
| `GET /api/connection` | Status, IP, remembered IP, camera, live view, camera-side setup steps |
| `POST /api/connection/reconnect` `{"ip"?, "forget"?}` | Drop the session and reconnect now (optionally to `ip`; `forget` clears the remembered camera) |

Live view endpoints take `?source=<name>` (default `wifi`) so other frame
sources can be added.

Aperture and shutter codes are APEX x 256: aperture `Av*256` (f = 2^(Av/2),
`939/256` = F3.5), shutter `Tv*256` (t = 2^-Tv s, `3328/256` = 1/8000).

### Files API (card browsing, download, import)

The camera serves its card only in play mode, and live view only runs in rec
mode. The first files request switches the camera to play mode and pauses live
view; `POST /api/files/done`, or 120 s without any files request, switches back
to rec mode and live view resumes. A running download or import keeps play
mode (the idle timer waits). Mode switches take the camera lock, so they never
interleave with a capture or setting change. If the camera refuses play mode
the API answers 409 with the camera's reply (see Known issues).

| Method & path | What |
|---|---|
| `GET /api/files?offset=0&limit=60&kind=all\|photo\|video` | `{total, items:[...]}`, newest first. `&refresh=1` re-reads the card |
| `GET /api/files/{id}/thumb` | Thumbnail JPEG (camera `DT` file, cached) |
| `GET /api/files/{id}/preview` | Large preview JPEG (camera `DL` file, cached; thumbnail if none) |
| `GET /api/files/{id}/download?variant=original\|raw` | Streams the file with `Content-Disposition: attachment; filename=...`; `Range` is passed through (206) |
| `POST /api/files/import` `{"ids":[...], "dest":"~/Pictures/Lumix", "include_raw":true}` | Background import, 202 + job. Files go to `<dest>/YYYY-MM-DD/<name>` (shot date); default dest `~/Pictures/Lumix`; a file with the same name and size is skipped; `.part` + Range resume on errors |
| `GET /api/jobs`, `GET /api/jobs/{id}` | Job progress: `state` (`queued`, `running`, `done`, `done_with_errors`, `failed`, `cancelled`), `files_total/done/skipped/failed`, `bytes_total/done`, `progress` 0..1, `current`, `saved`, `errors` |
| `POST /api/jobs/{id}/cancel` | Cancel an import |
| `POST /api/files/done` | Leave file browsing: rec mode + live view |
| `GET /api/files/status` | `{active, cammode, pins, idle_left_s, cached_items, last_error, jobs}` |

Card layout on the S5: the top level holds single files plus groups (time
lapse / burst, `pana:X_RecGroupType`, up to ~1000 frames each). The listing
pages through the top level (cheap) and expands only the groups a page touches,
so the first page of a 7980-file card takes ~5 s. Groups count as photos for
`kind`. Names are the card names (`P1024091.JPG` for the camera title
`102-4091`). `.MOV` clips carry no date and are imported into `undated/`.

Item: `{"id", "name", "kind": "photo"|"video", "date", "size", "has_raw",
"thumb_url", "preview_url", "download_url", "raw_url"?}`. `raw_url` is only
present for RAW+JPEG shots; a RAW-only shot has the `.RW2` as its original.
The WebSocket also pushes `files` (status) and `job` (finished job) messages.
Deleting files on the card is deliberately not implemented.

How it works: `camcmd playmode`, then UPnP ContentDirectory `Browse` on
`:60606/Server0/CDS_control` (recursive from ObjectID 0, 50 per page, listing
cached for the browsing session and re-validated by `TotalMatches`); files come
from `http://<camera>:50001/<DO|DL|DT><n>.<ext>`.

## Bluetooth (wake / Wi-Fi on / power-cycle) — experimental

Bluetooth LE control of the S5, reverse-engineered from LUMIX Sync 2.0.17 and
tested on the body (details: `docs/bluetooth-research.md`). Works from macOS
without OS-level pairing: the camera trusts a registered client id, not a bond.

What works: registering this Mac, then on every connection: clock sync, and
"join the home network" (verified once from Wi-Fi-off, with Wi-Fi back about
40 s later). Open: with Remote Wakeup ON, the camera accepts the wake
commands and goes to its "wakeup" state, but hasn't come back on Wi-Fi yet
(see the research doc, 1d). What doesn't: **a camera stuck in the `err_busy`
wedge can't be power-cycled by software.** `camcmd poweroff` answers
`err_reject` while wedged, LUMIX Sync has no BLE power-off, and a BLE Wi-Fi
restart doesn't clear the wedge. Power-cycle therefore only helps when
poweroff is accepted.

Setup:

```sh
/opt/homebrew/bin/python3 -m venv .venv-ble     # bleak needs Python >= 3.10 + pyobjc
.venv-ble/bin/pip install 'bleak>=1,<4'
bin/lumix-ble scan                              # bodies + state (normal/pairing/sleep...)
bin/lumix-ble register                          # once; camera on MENU > Setup > Bluetooth > SET > Pairing
bin/lumix-ble wake                              # Wi-Fi on, joined to the camera's saved home network
bin/lumix-ble status | dump | power-cycle
```

`register` does what LUMIX Sync does: BLE registration, then this Mac's Wi-Fi
joins the camera's own access point for about 30 s for the `accctrl`
handshake, then it forgets that AP and rejoins the home network. The camera
should end on "Pairing completed". The home SSID is read from the camera
(or `LUMIX_HOME_SSID`) and cached in `~/.config/lumix-remote/ble.json`.

`bin/lumix-ble` prints one JSON object per command. The server's `.venv` stays
aiohttp-only; it runs `bin/lumix-ble` as a subprocess.

API: `GET /api/ble/status` (`?probe=1` also connects), `POST /api/ble/wake`,
`POST /api/ble/power-cycle` (rec mode, `camcmd poweroff`, wait for the camera
to drop off, BLE wake, reconnect), `POST /api/ble/register`,
`POST /api/ble/watchdog {"enabled": bool}`.

Watchdog (off by default): `LUMIX_BLE_WATCHDOG=1 ./run.sh` tries a power-cycle
when the camera has been wedged for `LUMIX_BLE_WEDGE_AFTER` s (60), at most
`LUMIX_BLE_MAX_PER_HOUR` (3) times per hour, with backoff doubling from
`LUMIX_BLE_BACKOFF` s (120). Because of the wedge finding above, expect it to
report "camera refused poweroff" on a true wedge. `LUMIX_BLE_NAME` pins the
camera name. Camera side: Bluetooth ON, Remote Wakeup ON, home network saved,
LUMIX Sync closed on the phone (one BLE link at a time). Don't use the BLE
remote-shutter channel (`7be5fd56`): it turns the camera's Wi-Fi off. A process
started outside Terminal (launchd) needs its own Bluetooth permission.

## Layout

```
lumix/            camera client library (no web code)
  protocol.py     cam.cgi transport (serialised), reply parsing
  camera.py       Camera: discovery, handshake, subscription, keepalive, reconnect,
                  live view watchdog, typed commands, Bus (pub/sub), exclusive()
  events.py       UPnP GENA SUBSCRIBE + NOTIFY callback server
  liveview.py     UDP receiver, JPEG extraction, FrameSource (shared latest frame)
  menu.py         allmenu + curmenu -> generic settings model
  exposure.py     aperture/shutter code tables, lens limits
  discovery.py    SSDP listener, ddd probe, subnet scan
  presets.py      Auto preset: save manual values, apply, restore
  didl.py         ContentDirectory Browse SOAP + DIDL-Lite parser
  files.py        FileBrowser: play-mode session, listing cache, thumbs, downloads, import jobs
server/           aiohttp app (API, MJPEG, WebSocket) + static UI
  ui_api.py       /api/presets, /api/connection routes
  static/         web UI (index.html, app.js, style.css, manifest, icons)
lumixcli/         `lumix` command line client (HTTP API only)
bin/lumix         CLI wrapper (uses .venv)
tests/            unit tests: real S5 menu dumps, CLI against a fake server
docs/             protocol research notes
tools/            stand-alone protocol probe
```

### Connection sequence (what the server does)

It mirrors LUMIX Sync 2.0.17 (`docs/lumix-sync-wifi-flow.md`); every cam.cgi
request carries `User-Agent: LUMIX Sync` and only one is in flight at a time.

1. `GET :60606/Lumix/Server0/ddd` (device description).
2. `accctrl req_acc` with a LUMIX Sync-style id and device name, every 1 s while
   the camera answers `ok_under_research*`, until `ok,<name>,remote,...`.
3. `SUBSCRIBE :60606/Server0/CMS_event` (once per session, only while the camera
   is in play mode, which it is right after the accept), then 200 ms. The camera
   POSTs `NOTIFY` events (`X_Panasonic_Cam_Sync`: `busy`, `update`, `lens_Atta`,
   `mod_Play`, `mod_Rec`, ...). Without it the S5 never sends live view frames.
4. `setsetting raw_img_send`, `getinfo capability|allmenu|curmenu|lens`,
   `getsetting touch_type`, `getstate`, `setsetting device_name`.
5. Live view start, once: `stopstream`, `camcmd recmode` (only if not in rec),
   `startstream`.
6. `getstate` every second. `err_reject`, 3 failed polls or 30 s without a good
   reply = disconnected: `stopstream`, `UNSUBSCRIBE`, full reconnect. Renewal is
   by SID only; a refused renewal also means full reconnect.

Every command retries `err_busy` 5 times, 1 s apart, then reports it. Camera
events drive live view: `busy` (someone is using the body) stops it and blocks
shutter/video/AF with 409; `update` + `lens_Atta` after such a window restart it
once; `mod_Play` (camera switched to playback) is respected, nothing pulls the
camera back to rec except an explicit command or leaving the file browser. If
frames stop while in rec, the watchdog restarts the stream with a growing
back-off (10 s .. 120 s), never while the camera is busy or wedged.

### Extension points (timelapse, file browser, HDMI capture)

- **Camera lock:** `async with camera.exclusive("timelapse") as t:` serialises a
  multi-step operation against UI commands. Take it per step (one shot), not for
  the whole timelapse, so the UI stays usable between shots. `camera.op_owner`
  shows who holds it.
- **Mode switching:** `camera.ensure_rec()`; the file browser's
  `FileBrowser.ensure_play()` sets `camera.liveview_wanted = False` while
  browsing (the watchdog otherwise switches back to rec) and restores it in
  `leave()`.
- **Frame sources:** subclass `lumix.liveview.FrameSource`, call `publish(jpeg)`,
  register it in `app[SOURCES]["hdmi"]`; `/api/liveview.mjpg?source=hdmi` works.
- **Features:** append a `setup(app)` to `server.app.FEATURES` to add routes.
- **Events:** `camera.bus.publish(kind, data)` reaches every WebSocket client;
  `camera.log_event(kind, text)` adds to the event log.

## Known issues

- **camcmd wedge (S5):** the camera can get into a state where every `camcmd`
  (capture, video, recmode, playmode, AF) and often `startstream` answer
  `err_busy` until it is power-cycled; settings and touch keep working.
  Reproduced triggers: a `SUBSCRIBE` while the camera is in rec mode followed by
  `recmode` during the camera's own switch to play, and rapid `recmode`
  re-sends (our old watchdog and an event loop on `lens_Atta`). Nothing in-band
  clears it. The server avoids the triggers (see Connection sequence), flags
  `"wedged": true` in `/api/status` after two commands exhausted their retries,
  and calls `Camera.wedge_hooks` (the Bluetooth watchdog power-cycles from there).
- No Bluetooth: power-on and auto-connect are not possible.
- Kelvin white balance and photo-style fine adjustments (`__value__` parameters)
  are not editable yet.
- No authentication: keep the default 127.0.0.1 binding unless you trust the LAN.

## License

MIT, see `LICENSE`. Protocol knowledge comes from public reverse-engineering
notes; no third-party code is included.
