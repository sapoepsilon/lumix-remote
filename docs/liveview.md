# Live view: quality and frame endpoints

How the server gets live view from the camera over Wi-Fi, which sizes the S5
offers, and the HTTP endpoints other local programs (for example a macOS
virtual camera) read frames from.

## What the S5 can stream (real DC-S5, fw 2.03, 2026-10-12)

Live view size is the camera setting `liveviewsize`:

- `getsetting&type=liveviewsize` -> `vga` or `qvga`
- `setsetting&type=liveviewsize&value=vga|qvga` -> `ok`
- curmenu lists the choices as `menu_item_id_liveviewsize_vga` and
  `menu_item_id_liveviewsize_qvga` under `menu_item_id_liveview_quality`
- Anything else (`hd`) answers `err_param` and leaves the setting alone.

LUMIX Sync 2.0.17 has a "Live Control quality" setting, but it only shows it
when the camera's allmenu lists it, and the S5's allmenu doesn't. The app
offers no larger size, no bitrate and no fps option. `startstream` takes only
the UDP port.

| `liveviewsize` | JPEG | fps from the camera | kB/frame | Datagram | Stable |
|---|---|---|---|---|---|
| `vga` (default) | 640x360 | 16.8-17.4 (60 s: 1007 frames) | 26.0-26.3 | 1 per frame, ~27 KB, 19 IP fragments | yes: 0 unparsable, 0 lost fragments |
| `qvga` | 320x184 | 21.5-21.6 | 12.9-13.1 | 1 per frame, ~13.5 KB, 10 IP fragments | yes: 0 unparsable, 0 lost fragments |

Measured with `/api/liveview/stats` counters and 20-60 s reads of
`/api/liveview.mjpg` (evidence: `evidence/real-s5/real-s5-liveview-quality-2026-10-12.json`).
On 2026-10-11 the same `vga` stream ran at about 26 fps. The rate is set by the
camera and its Wi-Fi link, not the Mac. Frames come in bursts: about 0.6 s of
frames, then a ~0.73 s gap, at both sizes. Pinging the camera took 5-800 ms
(avg 268 ms), against 3-72 ms to the router. A stronger signal between the
camera and the router (closer, or 5 GHz) is the only lever left.

`vga` is the default, because `qvga` gets only ~25 % more frames for a quarter
of the pixels.

## How a size change is applied

- The camera reads `liveviewsize` at `startstream`. Setting it during a stream
  changes nothing until the stream is restarted. On 2026-10-12 a stream kept
  sending 640x360 for 20 s after `setsetting ... qvga` answered ok.
- `POST /api/liveview/quality` saves the choice for this camera
  (`liveview_quality` in its registry entry), then sends
  `setsetting liveviewsize`, waits 2 s, and does the normal live-view restart:
  `stopstream`, then `startstream`. No `recmode` is sent when the camera is
  already in rec.
- At connect, the server reads `getsetting liveviewsize` just before the one
  live-view start. If the camera holds a different size, it sends `setsetting`
  and waits 2 s. A camera left at `qvga` by another app comes back to the saved
  size this way.
- During an in-camera interval run the change is refused (409), the same as
  any other setting.

## Datagram format

There is one UDP datagram per frame at both sizes. Over 60 s the receiver's
datagram count matched its frame count exactly, and IP reassembly was 1:1.
Frames never span several UDP datagrams; the kernel reassembles the IP
fragments, so the parser needs no multi-packet support.

| Bytes | Meaning |
|---|---|
| 0-1 | Datagram length, big-endian (0x695d = 26973) |
| 9-14 | Camera MAC |
| 15-16 | `0x0021` = image frame |
| 30-31 | BE16 `0x00de`: the JPEG starts at value + 32 = 254 |

The header has no frame counter (only bytes 0-1 change between frames), so lost
frames can't be counted from it. `/api/liveview/stats` shows `datagrams`,
`bad` (unparsable) and the latest header in hex.

## Endpoints for local consumers

All of them also exist as `/api/cameras/{id}/...`. The server listens on
`127.0.0.1:8770` by default.

| Endpoint | Use |
|---|---|
| `GET /api/liveview.mjpg` | `multipart/x-mixed-replace; boundary=lumixframe`. Each part has `Content-Type: image/jpeg` and `Content-Length`. Frames are sent as they arrive, and a slow reader skips to the newest frame. This is the easiest input for a virtual camera. |
| `GET /api/liveview.jpg` | The latest JPEG. Headers: `X-Frame-Seq` (increasing frame number), `X-Frame-Width`, `X-Frame-Height`, `Cache-Control: no-store`. 503 if no frame came within 5 s. |
| `GET /api/liveview.jpg?after=<seq>` | Waits up to 5 s for a frame newer than `<seq>`, so a poller gets each frame once. 503 `no newer frame` on timeout, for example when the stream stopped. |
| `GET /api/liveview/stats` | `{fps, seq, age, bytes, width, height, viewers, datagrams, bad, datagram_bytes, header_bytes, header_hex, liveview_quality}` |
| `GET /api/liveview/quality` | `{current, wanted, options, default}`, e.g. `{"current": "vga", "wanted": "vga", "options": ["vga", "qvga"], "default": "vga"}` |
| `POST /api/liveview/quality` `{"liveview_quality": "vga"}` | Save + apply (see above). Returns `{result, applied, restarted, liveview_quality}`. 400 for a size the camera doesn't offer; 409 when busy or during an interval run. |
| `GET /api/status` | Also carries `liveview` (fps, width, height, bytes, age) and `liveview_quality`. |

The frame size can change when the quality changes, so read the width and
height from each JPEG (or from `X-Frame-Width`/`X-Frame-Height`) and don't
assume a fixed size. `age` in the stats says how old the last frame is. Frames
stop while the camera is in playback, file browsing or busy on the body; the
server restarts live view by itself afterwards.

The web UI has the same switch under Controls -> Live view -> Live view quality.
