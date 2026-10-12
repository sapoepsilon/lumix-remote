# Lumix S5 (DC-S5) Wi-Fi protocol research

Compiled 2026-10-11 from public sources. Nothing was tested on a real S5. Panasonic publishes no spec for this API (the beta LUMIX SDK is USB/Windows). Everything here is reverse-engineered by third parties. Tags: [S5IIX] = confirmed on S5IIX, [G9II] = G9II, [old] = GH3/GH4/G80/GX80-era, [?] = unverified.

## 1. Transport and pairing
- Control: HTTP GET `http://<ip>/cam.cgi?mode=<m>&type=<t>&value=<v>&value2=<v2>`, port 80. Replies are XML `<camrply><result>ok</result>...</camrply>` (`Content-Type: text/xml`, `Server: Panasonic`), or CSV text `ok,a,b,c` for accctrl.
- Side services: UPnP device description `http://<ip>:60606/Lumix/Server0/ddd` (S5IIX also answers 404 on `:60606/`); ContentDirectory SOAP at `:60606/Server0/CDS_control`; files at `:50001/<name>`.
- Camera must be in Wi-Fi "Remote shooting" mode. In AP mode the first connection pops an approve dialog on the camera (lumix-link-desktop README).
- Three handshake generations exist. Try them in this order (as A01d does):
  1. Legacy [old]: `cam.cgi?mode=accctrl&type=req_acc&value=<uuid>&value2=<device name>` -> `ok,<camera name>,remote,encrypted`. libgphoto2 uses `value=0&value2=libgphoto2/lumix`. lumix-link-desktop works around `err-unsuitable-app` by calling `req_acc&value=0&value2=Vexia%20Fcs` first.
  2. `type=req_acc_a` (same params), used by A01d as the second try.
  3. Newer cameras [S5IIX, G9II]: `type=req_acc_g` -> `ok,<8 hex chars>` (nonce). Then `type=req_acc_e&value=<hex>&value2=<hex>` repeatedly until `ok`. While waiting for the user to approve on the camera it answers `ok_under_research_no_msg`; retry every ~1 s. Result: `ok,<friendlyName>,remote,open,<SESSION_ID>`.
     - Variant A [S5IIX, liblumix]: value/value2 = lowercase hex of the UDN (from `ddd`, without `uuid:`) and of the device name.
     - Variant B [G9II, Aikhjarto `hash_wifi`]: nonce = little-endian uint32 of the req_acc_g hex. `value` = hex of 36 bytes, for each of 9 constants C in [892617780, 808663348, 808529965, 808529200, 942485552, 758198320, 809579056, 842018864, 942748980] append big-endian uint32(nonce XOR C). `value2` = hex of big-endian uint32(nonce XOR 4281684038). Aikhjarto calls req_acc_e twice (first reply has 4 fields, second carries the session id).
     - A01d's last resort sends MD5 hex of the UUID and of the name as value/value2 [?].
  4. After success: send header `X-SESSION_ID: <id>` on every cam.cgi request, then `setsetting&type=device_name&value=<name>` (G9II Python sends `User-Agent: LUMIX Sync`).
- Bluetooth: LUMIX Sync uses BLE for wake-up/auto-connect (Aikhjarto has a separate BLE module, `bleak`). No source says Wi-Fi control needs BLE once the camera is on and in Wi-Fi mode. liblumix (S5IIX) and Aikhjarto's Wi-Fi path work with Wi-Fi only. S5 original: [?].
- Session timeout: the camera drops the app if no command arrives. liblumix polls `getstate` every 1 s, GMaster and Aikhjarto every 2 s, libgphoto2 on each live-view frame.

## 2. Commands (all `cam.cgi?`; add `X-SESSION_ID` header on newer bodies)
Mode switch (required first): `mode=camcmd&value=recmode` | `value=playmode`. Many things fail with the wrong mode (live view and capture need recmode; content browsing needs playmode, wait ~1 s).
| Purpose | Query |
|---|---|
| State / heartbeat | `mode=getstate` (XML with `<sd_access>`, battery, mode, ...) |
| Capabilities | `mode=getinfo&type=capability` (also gives file-format ids); `type=allmenu` (menu tree, each item has cmd_mode/cmd_type/cmd_value, i.e. the exact setsetting strings); `type=curmenu` (enabled flags); `type=lens` |
| Photo | `mode=camcmd&value=capture`; abort bulb/long exp: `value=capture_cancel` |
| Video | `mode=camcmd&value=video_recstart` / `video_recstop` |
| Power | `mode=camcmd&value=poweroff` |
| ISO | `mode=setsetting&type=iso&value=auto` or `400` |
| Aperture | `mode=setsetting&type=focal&value=392/256` (f1.7), `598/256` (f2.2), `2304/256` (f22). Value = 256 * log2-ish step code; GMaster ApertureBinary table: 512=f2, 768=f2.8, 1024=f4, 1280=f5.6, 1536=f8, 2048=f16 |
| Shutter | `mode=setsetting&type=shtrspeed&value=<n>/256` (liblumix bulb uses `16384/256`); codes in GMaster `ShutterBinary` (3584=1/16000...). Use allmenu for the exact list [?] |
| Exposure comp | `mode=setsetting&type=exposure&value=-1/3` (also `0`, `5/3`, `1`, `8/3`) [GX80] |
| Focus mode | `mode=getsetting&type=focusmode`; `setsetting&type=focusmode&value=afs|aff|afc|mf` |
| AF mode | `setsetting&type=afmode&value=facedetection` (others via allmenu) |
| White balance | not documented in any source; read `allmenu`/`curmenu` for its cmd_type/cmd_value [?] |
| Other settings seen | `peaking` (high/low/off), `colormode`, `filter_setting`, `lightmetering` (center/spot/multi), `videoquality`, `liveviewsize`, `raw_img_send` (on/off, G9II, for playback) |
| One-shot AF | `mode=camcmd&value=oneshot_af` |
| Touch AF / focus point | `mode=camctrl&type=touch&value=<x>/<y>&value2=on`, x,y 0..1000, release `camcmd&value=touchrelease` (G9II). Older: `type=touchaf&value=x/y`, `touchcapt&value=840/234&value2=on` (touch + shoot), off: `value=0/0&value2=off`. Drag: `type=touch_trace&value=start|continue|stop&value2=x/y`. `getsetting&type=touch_type` tells which style applies |
| AF area size / pinch | `type=touchaf_chg_area`, `type=pinch&value=<stage>&value2=x1/y1/x2/y2` (GMaster) |
| Manual focus | `mode=camctrl&type=focus&value=wide-normal|wide-fast|tele-normal|tele-fast`; MF assist `type=mf_asst&value=x/y`, `asst_disp`, `setsetting&type=mf_asst_mag` |
| Zoom (power zoom lenses only) | `mode=camcmd&value=wide-normal|wide-fast|tele-normal|tele-fast|zoomstop` |
| Misc | `camcmd&value=autoreviewunlock`, `menu_entry`, `lcd_on` |
Playback / download:
- `mode=camcmd&value=playmode`, then `mode=get_content_info` -> `<total_content_number>`, `<current_position>`, `<content_number>`.
- UPnP browse: POST `http://<ip>:60606/Server0/CDS_control`, headers `SOAPACTION: "urn:schemas-upnp-org:service:ContentDirectory:1#Browse"`, `Content-Type: text/xml; charset="utf-8"`, `User-Agent: Panasonic Android/1 DM-CP`. Body: standard `u:Browse` with `ObjectID=0`, `BrowseDirectChildren`, `Filter=*`, `StartingIndex`, `RequestedCount`, plus `<pana:X_FromCP>LumixLink2.0</pana:X_FromCP>` (xmlns:pana="urn:schemas-panasonic-com:pana"). The reply `Result` holds DIDL-Lite. Containers (e.g. `01111980DIR`) nest items.
- Items list `<res>` URLs on port 50001, e.g. `DO01111793.RW2`/`.JPG` (original), `DL...JPG` (medium), `DT...JPG` (thumbnail), `.MP4` for video. Plain HTTP GET (support Range for resume; downloads can stall, GMaster/libgphoto2 resume).
- On G9II, `setsetting&type=raw_img_send&value=on` is needed in playmode to expose RAW.

## 3. Live view
- `mode=camcmd&value=recmode`, bind a UDP socket, then `mode=startstream&value=<udp port>` (49152 and 49199 are used by implementations; avoid 50001/60606). Stop: `mode=stopstream`.
- Camera sends UDP datagrams to the client IP/port. Each datagram is one frame: a binary header then a JPEG (SOI `FF D8 FF` ... EOI `FF D9`). Simplest robust parse (pylumix, Aikhjarto, libgphoto2): find the first `FF D8`, take until the last `FF D9`.
- GMaster [GH3/GH4]: JPEG start = big-endian uint16 at byte offset 30, plus 32; sanity-check `imageStart > 60`. The header before that also carries exposure info (ISO/shutter/aperture at fixed offsets, model-specific). Header length therefore varies; do not hard-code.
- Size VGA 640x480 (QVGA 320x240 or 320x184 on some bodies). Frames fit in one datagram in practice (recv buffer 65536).
- Keep-alive: `getstate` every 1-2 s (liblumix 1 s; pylumix on each 2 s UDP timeout). Without it the camera stops streaming and drops the session.
- Stream only runs in recmode, and not while in playmode or menus.

## 4. Network / discovery
- AP (direct) mode: camera is `192.168.54.1` (all projects).
- Via-network mode: camera joins the home router, gets a DHCP address. Discover by SSDP: UDP multicast `239.255.255.250:1900`, `M-SEARCH * HTTP/1.1`, `MAN: "ssdp:discover"`, `MX: 2`, `ST: urn:schemas-upnp-org:device:MediaServer:1` (A01d, GMaster/RSSDP). Filter responses whose `LOCATION` path is `/Lumix/Server0/ddd` (Aikhjarto) or `modelName == LUMIX`. Fetch the `ddd` XML for friendlyName, modelName, modelNumber, serialNumber, UDN, `pana:X_FirmVersion`. Aikhjarto's docstring says the G9II is reachable by hostname `mlbel` on a router network (name resolution mechanism unknown, not verified for S5).
- macOS needs the Local Network privacy permission for multicast and the camera; ship an `NSLocalNetworkUsageDescription` and `NSBonjourServices` if using Network.framework.
- Camera joins one network at a time; in AP mode the Mac loses its normal internet unless on a second interface.

## 5. Existing projects
| Project | Lang / license | Last push | Camera evidence |
|---|---|---|---|
| njfdev/liblumix | C++, no license | 2024-06 | **S5IIX** (LUMIX Sync captured with Wireshark); req_acc_g/e, X-SESSION_ID, ddd, CDS browse, RAW download, 1 s getstate. https://github.com/njfdev/liblumix |
| Aikhjarto/LUMIX-G9II-Remote-Control | Python, GPL-3.0 | 2024-12 | **G9II**; most complete (hash_wifi, SSDP, live view, playback, BLE). https://github.com/Aikhjarto/LUMIX-G9II-Remote-Control |
| abichinger/pylumix | Python, MIT | 2026-02 | "Image App capable"; legacy req_acc, live view, CDS browse, download (AI-assisted, untested on S-series). https://github.com/abichinger/pylumix |
| Rambalac/GMaster | C#, none stated | 2022-12 | GH3, GH4, G7, G80, GX7/80/85; best packet-header code. https://github.com/Rambalac/GMaster |
| MRSa/A01d | Java/Kotlin, none | 2026-08 | generic Panasonic, includes the req_acc ladder + SSDP. https://github.com/MRSa/A01d |
| cleverfox/lumixproto | notes, none | 2018 | GX80; cleanest command list. https://github.com/cleverfox/lumixproto |
| peci1/lumix-link-desktop | JS/Java, BSD-3 (archived 2023) | 2021 | GH3 full, GH4/G6/GM1/FZ1000/GX80 partial. https://github.com/peci1/lumix-link-desktop |
| gphoto/libgphoto2 `camlibs/lumix` | C, LGPL-2.1 | active | G80, GH4, GX9, GH5S, G70, TZ91. https://github.com/gphoto/libgphoto2/issues/409 |
| totoantibes/LumixCameraAscomDriver | VB.NET, CC BY-ND 3.0 | - | G80, GH4, GH5S tested; S1 untested; no live view. https://github.com/totoantibes/LumixCameraAscomDriver |
| palmdalian/python_lumix_control | Python, MIT | 2017 | G85-era. https://github.com/palmdalian/python_lumix_control |
- Not credible: larszu/lz-camera-bridge claims S5/GH6 support but its client is invented (token, `menu set iris_...`), do not trust. williamwu1234/lumix-s1m2-research and ShaoCI-Hz/lumix-fullframe-reverse cover firmware/USB, no Wi-Fi protocol found. Found nothing tested on S1, original S5, GH5, or GH6 over Wi-Fi.
- Official alternatives: LUMIX Tether (S5 supported, USB, Windows/macOS) and LUMIX SDK; not Wi-Fi.
- Licensing: GPL-3.0 (Aikhjarto) and CC BY-ND code must not be copied into a permissive repo; reimplement from protocol facts.

## 6. Unknowns to test on the real S5
1. Which handshake the S5 (firmware 2.x/3.x) accepts: plain `req_acc`, `req_acc_g`+`req_acc_e` variant A (hex UDN/name) or variant B (hash_wifi). Record every raw reply.
2. Whether pairing needs BLE or only the on-camera approval dialog; behaviour of the first-time connect dialog.
3. Is `X-SESSION_ID` mandatory or does the camera also bind by client IP.
4. Exact `setsetting` type/value strings for WB (incl. Kelvin), shutter, ISO list, exposure comp, aperture on S5; dump `allmenu`, `curmenu`, `capability` once and archive them.
5. Live-view header length/layout on S5, frame size (S5 may send larger than VGA), fragmentation, whether `FF D8` can appear in the header.
6. Touch AF coordinate space and which of `touch`/`touchaf`/`touchcapt` works (`getsetting&type=touch_type`).
7. Whether the S5 supports power zoom/`zoomstop` (lens dependent) and focus-by-wire `camctrl&type=focus` steps.
8. Session timeout length, recovery after Wi-Fi drop, and behaviour when the camera auto-sleeps.
9. Via-network mode: does it answer SSDP on the same subnet, multicast via Mac Wi-Fi, mDNS name; AP-mode behaviour with macOS Local Network permission.
10. Playback: Browse paging limits, RAW (`.RW2`) availability, HLG/V-Log video file URLs, download stalls and Range support.
11. Whether live view works while recording video and during capture, and if photo capture needs recmode re-entry.
12. Whether the original DC-S5 uses the same handshake as the S5IIX (the only liblumix evidence).
