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

- Live view (MJPEG, ~26 fps at 640x360 on the S5), click/tap to focus there.
- Big shutter button (Space), video record start/stop (R) with a timer.
- Quick controls: aperture, shutter, exposure mode, ISO, exposure compensation,
  white balance, focus mode, drive mode, photo quality/size, video quality.
- "All settings": generated from the camera's own menu description, so it shows
  whatever your body and firmware expose, with filter.
- Connection status, battery, card, remaining shots/video, lens, event log.

Works at phone width.

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
| `GET /api/ws` | WebSocket push: `status`, `exposure`, `settings` (changed ids), `event`, `events` |

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
server/           aiohttp app (API, MJPEG, WebSocket) + static UI
tests/            unit tests with real S5 menu dumps (python -m unittest)
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
