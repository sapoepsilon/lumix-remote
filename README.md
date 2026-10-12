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

"Power off" sends `camcmd poweroff` after a confirm dialog. **The camera cannot
be switched back on over Wi-Fi** (Bluetooth wake-up is not implemented), so you
will need to turn it on by hand.

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

1. `GET :60606/Lumix/Server0/ddd` (device description).
2. `accctrl req_acc` with a LUMIX Sync-style id and device name; retry while the
   camera answers `ok_under_research*` until `ok,<name>,remote,...`.
3. `setsetting device_name`.
4. `SUBSCRIBE :60606/Server0/CMS_event` with a callback to our event server; the
   camera then POSTs `NOTIFY` events (`X_Panasonic_Cam_Sync`: `lens_Atta`,
   `mod_Play`, ...). On the S5 this is what gets live view going after a cold start.
5. `getinfo capability|allmenu|curmenu|lens`, `getsetting touch_type`.
6. `camcmd recmode` (`err_busy` can just mean "already in rec"), `startstream`.
7. `getstate` every second as keepalive; subscription renewed before expiry;
   live view restarted if frames stop; full reconnect after 3 failed keepalives.

### Extension points (timelapse, file browser, HDMI capture)

- **Camera lock:** `async with camera.exclusive("timelapse") as t:` serialises a
  multi-step operation against UI commands. Take it per step (one shot), not for
  the whole timelapse, so the UI stays usable between shots. `camera.op_owner`
  shows who holds it.
- **Mode switching:** `camera.ensure_rec()` exists; a file browser would add an
  `ensure_play()` and set `camera.liveview_wanted = False` while browsing (the
  watchdog otherwise switches back to rec), restoring it afterwards.
- **Frame sources:** subclass `lumix.liveview.FrameSource`, call `publish(jpeg)`,
  register it in `app[SOURCES]["hdmi"]`; `/api/liveview.mjpg?source=hdmi` works.
- **Features:** append a `setup(app)` to `server.app.FEATURES` to add routes.
- **Events:** `camera.bus.publish(kind, data)` reaches every WebSocket client;
  `camera.log_event(kind, text)` adds to the event log.

## Known issues

- On the test S5, after a `mod_Play` event the camera started answering
  `err_busy` to every `camcmd` (capture, video, recmode, playmode, oneshot_af)
  while settings, touch and live view kept working. The UI shows a hint. A camera
  power cycle (by hand) is the suspected fix; not yet confirmed.
- No Bluetooth: power-on and auto-connect are not possible.
- Kelvin white balance and photo-style fine adjustments (`__value__` parameters)
  are not editable yet.
- No authentication: keep the default 127.0.0.1 binding unless you trust the LAN.

## License

MIT, see `LICENSE`. Protocol knowledge comes from public reverse-engineering
notes; no third-party code is included.
