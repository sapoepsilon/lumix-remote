"""Camera emulator (emulator/) against a minimal client: handshake, GENA events,
wedge triggers, live-view datagrams, Browse paging, Range downloads.

Self-contained on purpose: it talks HTTP/UDP directly and does not import lumix/,
so it keeps working while the client is refactored.
"""

import asyncio
import re
import struct
import time
import unittest
import xml.etree.ElementTree as ET

import aiohttp
from aiohttp import web

from emulator.camera import EmuConfig, Identity
from emulator.jpeg import jpeg_size
from emulator.servers import EmulatorNode

UUID = "4D454930-0100-1000-8001-0123456789AB"


def fast_cfg(**kw) -> EmuConfig:
    base = dict(settle_min=0.6, settle_max=0.8, approval_delay_s=0.3, capture_busy_s=0.4,
                video_busy_s=0.4, rec_switch_s=0.05, sub_play_switch_s=0.1, cds_ready_s=0.1,
                session_timeout_s=0, seed=7)
    base.update(kw)
    return EmuConfig(**base)


async def raw_request(port: int, method: str, path: str, headers, body: bytes = b""):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    req = "%s %s HTTP/1.1\r\nHOST: 127.0.0.1:%d\r\n%s" % (
        method, path, port, "".join("%s: %s\r\n" % kv for kv in headers))
    if body:
        req += "Content-Length: %d\r\n" % len(body)
    w.write(req.encode() + b"\r\n" + body)
    await w.drain()
    data = await asyncio.wait_for(r.read(), 5)
    w.close()
    text = data.decode("utf-8", "replace")
    status = int(re.match(r"HTTP/1\.\d (\d+)", text).group(1))
    hdrs = {}
    for line in text.split("\r\n")[1:]:
        if not line:
            break
        k, _, v = line.partition(":")
        hdrs[k.strip().upper()] = v.strip()
    return status, hdrs, text.split("\r\n\r\n", 1)[-1]


class Events:
    """A NOTIFY callback server, like the client's EventListener."""

    def __init__(self):
        self.items = []
        self.cond = asyncio.Condition()
        self.port = 0
        self._runner = None

    async def start(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        await self._runner.cleanup()

    async def _handle(self, req):
        body = await req.read()
        vals = []
        for prop in ET.fromstring(body):
            for var in prop:
                vals.append((var.tag, (var.text or "").strip()))
        async with self.cond:
            self.items.append({"method": req.method, "sid": req.headers.get("SID"),
                               "seq": int(req.headers.get("SEQ", "-1")), "vals": vals,
                               "t": time.monotonic()})
            self.cond.notify_all()
        return web.Response()

    def values(self):
        return [v for it in self.items for (k, v) in it["vals"] if k == "X_Panasonic_Cam_Sync"]

    async def wait_for(self, value, timeout=5.0):
        async with self.cond:
            await asyncio.wait_for(self.cond.wait_for(lambda: value in self.values()), timeout)


class UdpSink(asyncio.DatagramProtocol):
    def __init__(self):
        self.packets = []

    def datagram_received(self, data, addr):
        self.packets.append((time.monotonic(), data, addr))


class EmuTestBase(unittest.IsolatedAsyncioTestCase):
    cfg_kw = {}

    async def asyncSetUp(self):
        self.node = await EmulatorNode("127.0.0.1", 0, 0, 0, 0, Identity.for_index(0),
                                       fast_cfg(**self.cfg_kw), card_photos=3,
                                       card_group=120).start()
        self.cam = self.node.cam
        self.http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True))
        self.events = await Events().start()

    async def asyncTearDown(self):
        await self.http.close()
        await self.events.stop()
        await self.node.stop()

    # ---- helpers
    def port(self, k):
        return self.node.ports[k]

    async def cgi(self, **params):
        url = "http://127.0.0.1:%d/cam.cgi" % self.port("http")
        async with self.http.get(url, params=params) as r:
            return (await r.read()).decode()

    async def result(self, **params):
        t = await self.cgi(**params)
        m = re.search(r"<result>([^<]*)</result>", t)
        return m.group(1) if m else t.split(",")[0]

    async def control(self, path, method=None, **params):
        method = method or ("GET" if path in ("state", "log") else "POST")
        url = "http://127.0.0.1:%d/emu/%s" % (self.port("control"), path)
        async with self.http.request(method, url, params=params) as r:
            return await r.json()

    async def connect(self):
        first = await self.cgi(mode="accctrl", type="req_acc", value=UUID, value2="Test")
        self.assertTrue(first.startswith("ok_under_research_no_msg,S5-"), first)
        for _ in range(20):
            await asyncio.sleep(0.1)
            r = await self.cgi(mode="accctrl", type="req_acc", value=UUID, value2="Test")
            if r.startswith("ok,"):
                return r
        self.fail("req_acc never became ok")

    async def subscribe(self):
        st, h, _ = await raw_request(self.port("upnp"), "SUBSCRIBE", "/Server0/CMS_event", [
            ("User-Agent", "Panasonic Android/1 DM-CP"),
            ("CALLBACK", "<http://127.0.0.1:%d/Camera/event>" % self.events.port),
            ("NT", "upnp:event"), ("TIMEOUT", "Second-300")])
        self.assertEqual(st, 200)
        return h["SID"]

    async def ready_in_rec(self):
        """The safe, LUMIX Sync-like sequence: accept, SUBSCRIBE in play, wait for mod_Play, recmode."""
        await self.connect()
        await self.subscribe()
        await self.events.wait_for("mod_Play")
        self.assertEqual(await self.result(mode="camcmd", value="recmode"), "ok")
        await asyncio.sleep(0.15)


class HandshakeTest(EmuTestBase):
    async def test_ddd_is_scrubbed_and_points_at_our_ports(self):
        async with self.http.get("http://127.0.0.1:%d/Lumix/Server0/ddd" % self.port("upnp")) as r:
            ddd = await r.text()
            self.assertEqual(r.headers["SERVER"], "Panasonic/1.0 UPnP/1.0 Panasonic-UPnP-MW/1.0")
        # only the fake identity and the emulator's own address appear
        self.assertIn("<friendlyName>S5-E00001</friendlyName>", ddd)
        self.assertIn("<serialNumber>EMU0000001</serialNumber>", ddd)
        self.assertIn("<UDN>uuid:4D454930-0100-1000-8000-024C58E00001</UDN>", ddd)
        self.assertEqual(set(re.findall(r"http://([^/]+)/", ddd)),
                         {"127.0.0.1:%d" % self.port("upnp")})
        self.assertIn("<modelNumber>DC-S5</modelNumber>", ddd)
        self.assertIn("http://127.0.0.1:%d/Server0/CMS_event" % self.port("upnp"), ddd)

    async def test_req_acc_research_then_ok(self):
        st = await self.cgi(mode="getstate")          # answered before req_acc, like the S5
        self.assertIn("<cammode>play</cammode>", st)
        self.assertEqual(await self.result(mode="camcmd", value="recmode"), "err_reject")
        r = await self.connect()
        self.assertEqual(r, "ok,S5-E00001,remote,encrypted")
        self.assertEqual(await self.result(mode="setsetting", type="device_name", value="Test"), "ok")
        self.assertEqual(await self.cgi(mode="accctrl", type="req_acc_g"), "err_param")
        cap = await self.cgi(mode="getinfo", type="capability")
        self.assertIn("<appname>LUMIX_Sync</appname>", cap)
        lens = await self.cgi(mode="getinfo", type="lens")
        self.assertTrue(lens.startswith("ok,2304/256,939/256"))
        self.assertIn('touch_type="5"', await self.cgi(mode="getsetting", type="touch_type"))
        self.assertEqual(await self.result(mode="getsetting", type="mf_assist"), "err_critical")

    async def test_subscribe_notify_renew_unsubscribe(self):
        await self.connect()
        sid = await self.subscribe()
        self.assertRegex(sid, r"^uuid:4D454931-[0-9A-F]{4}-1000-8000-024C58E00001$")
        await self.events.wait_for("mod_Play")
        first = self.events.items[0]
        self.assertEqual(first["seq"], 0)
        self.assertIn(("CurrentConnectionIDs", "0"), first["vals"])
        self.assertEqual(self.events.items[-1]["seq"], 1)
        delay = self.events.items[-1]["t"] - first["t"]
        self.assertGreater(delay, 0.4)                # settle window, not instant
        st, _, _ = await raw_request(self.port("upnp"), "SUBSCRIBE", "/Server0/CMS_event",
                                     [("SID", sid), ("TIMEOUT", "Second-300")])
        self.assertEqual(st, 200)
        st, _, _ = await raw_request(self.port("upnp"), "UNSUBSCRIBE", "/Server0/CMS_event",
                                     [("SID", "uuid:4D454931-0001-1000-8000-024C58E00001")])
        self.assertEqual(st, 412)
        st, _, _ = await raw_request(self.port("upnp"), "UNSUBSCRIBE", "/Server0/CMS_event",
                                     [("SID", sid)])
        self.assertEqual(st, 200)

    async def test_session_drop_then_getstate_err_reject(self):
        await self.connect()
        self.assertEqual(await self.result(mode="getstate"), "ok")
        await self.control("drop-session")
        self.assertEqual(await self.result(mode="getstate"), "err_reject")
        self.assertEqual(await self.result(mode="getinfo", type="curmenu"), "err_reject")
        await self.connect()
        self.assertEqual(await self.result(mode="getstate"), "ok")

    async def test_idle_session_timeout(self):
        await self.control("config", session_timeout_s="0.3")
        await self.connect()
        await asyncio.sleep(0.5)
        self.assertEqual(await self.result(mode="getstate"), "err_reject")


class WedgeTest(EmuTestBase):
    async def test_recmode_before_mod_play_wedges(self):
        await self.connect()
        await self.subscribe()
        self.assertEqual(await self.result(mode="camcmd", value="recmode"), "err_critical")
        st = await self.control("state")
        self.assertTrue(st["wedged"])
        await self.events.wait_for("mod_Rec")     # queued lens_Atta, mod_Play, mod_Rec arrive
        self.assertEqual(self.events.values()[:3], ["lens_Atta", "mod_Play", "mod_Rec"])
        await asyncio.sleep(1.0)                  # long after the settle window
        for v in ("recmode", "playmode", "capture", "oneshot_af"):
            self.assertEqual(await self.result(mode="camcmd", value=v), "err_busy", v)
        self.assertEqual(await self.result(mode="camcmd", value="bogus"), "err_param")
        self.assertEqual(await self.result(mode="setsetting", type="iso", value="400"), "ok")
        self.assertIn('iso="400"', await self.cgi(mode="getsetting", type="iso"))
        # startstream alternates ok / err_busy and never delivers a frame
        res = [await self.result(mode="startstream", value="49999") for _ in range(4)]
        self.assertEqual(sorted(set(res)), ["err_busy", "ok"])
        # only a power cycle clears it
        await self.control("power-cycle", off_s="0.1")
        st = await self.control("state")
        self.assertFalse(st["wedged"])
        self.assertEqual(st["cammode"], "play")

    async def test_stopstream_before_mod_play_wedges(self):
        await self.connect()
        await self.subscribe()
        self.assertEqual(await self.result(mode="stopstream"), "err_critical")
        self.assertTrue((await self.control("state"))["wedged"])

    async def test_stopstream_without_stream_is_err_critical_but_harmless(self):
        await self.connect()
        self.assertEqual(await self.result(mode="stopstream"), "err_critical")
        self.assertFalse((await self.control("state"))["wedged"])

    async def test_subscribe_in_rec_switches_to_play(self):
        await self.connect()
        self.assertEqual(await self.result(mode="camcmd", value="recmode"), "ok")
        await asyncio.sleep(0.1)
        self.assertIn("<cammode>rec</cammode>", await self.cgi(mode="getstate"))
        await self.subscribe()
        await asyncio.sleep(0.25)
        self.assertIn("<cammode>play</cammode>", await self.cgi(mode="getstate"))
        self.assertNotIn("mod_Play", self.events.values())   # silent switch first

    async def test_recmode_churn_wedges(self):
        await self.ready_in_rec()
        res = []
        for _ in range(3):
            await asyncio.sleep(0.1)
            res.append(await self.result(mode="camcmd", value="recmode"))
        self.assertEqual(res[:2], ["ok", "ok"])
        self.assertEqual(res[2], "err_critical")
        self.assertTrue((await self.control("state"))["wedged"])
        await self.events.wait_for("mod_Play")

    async def test_capture_busy_window(self):
        await self.ready_in_rec()
        before = (await self.control("state"))["card"]["total"]
        self.assertEqual(await self.result(mode="camcmd", value="capture"), "ok")
        self.assertIn("<sd_access>on</sd_access>", await self.cgi(mode="getstate"))
        self.assertEqual(await self.result(mode="camcmd", value="capture"), "err_busy")
        await asyncio.sleep(0.5)
        self.assertEqual(await self.result(mode="camcmd", value="capture"), "ok")
        self.assertEqual((await self.control("state"))["card"]["total"], before + 2)

    async def test_touch_needs_release(self):
        await self.ready_in_rec()
        self.assertEqual(await self.result(mode="camctrl", type="touch", value="500/500",
                                           value2="on"), "ok")
        self.assertEqual(await self.result(mode="camctrl", type="touch", value="500/500",
                                           value2="on"), "err_reject")
        self.assertEqual(await self.result(mode="camctrl", type="touch", value="0/0",
                                           value2="off"), "ok")

    async def test_poweroff(self):
        await self.ready_in_rec()
        self.assertEqual(await self.result(mode="camcmd", value="playmode"), "ok")
        await asyncio.sleep(0.1)
        self.assertEqual(await self.result(mode="camcmd", value="poweroff"), "err_reject")
        self.assertEqual(await self.result(mode="camcmd", value="recmode"), "ok")
        await asyncio.sleep(0.1)
        self.assertEqual(await self.result(mode="camcmd", value="poweroff"), "ok")
        await asyncio.sleep(0.8)
        with self.assertRaises((aiohttp.ClientError, OSError)):
            await self.cgi(mode="getstate")
        await self.control("power-on")
        self.assertIn("<cammode>play</cammode>", await self.cgi(mode="getstate"))

    async def test_body_busy_events(self):
        await self.ready_in_rec()
        await self.control("body/busy")
        await self.events.wait_for("busy")
        self.assertEqual(await self.result(mode="camcmd", value="capture"), "err_busy")
        await self.control("body/rec")
        await self.events.wait_for("update")
        await asyncio.sleep(0.3)
        self.assertEqual(await self.result(mode="camcmd", value="oneshot_af"), "ok")


class RealismOffTest(EmuTestBase):
    cfg_kw = {"wedge_realism": False}

    async def test_no_wedge_without_realism(self):
        await self.connect()
        await self.subscribe()
        self.assertEqual(await self.result(mode="camcmd", value="recmode"), "ok")
        self.assertEqual(await self.result(mode="stopstream"), "ok")
        self.assertFalse((await self.control("state"))["wedged"])


class LiveViewTest(EmuTestBase):
    async def _sink(self):
        loop = asyncio.get_running_loop()
        tr, proto = await loop.create_datagram_endpoint(UdpSink, local_addr=("127.0.0.1", 0))
        return tr, proto, tr.get_extra_info("socket").getsockname()[1]

    async def test_frames_after_safe_sequence(self):
        tr, sink, port = await self._sink()
        try:
            await self.ready_in_rec()
            self.assertEqual(await self.result(mode="startstream", value=str(port)), "ok")
            t0 = time.monotonic()
            while time.monotonic() - t0 < 1.2:
                await asyncio.sleep(0.3)
                await self.cgi(mode="getstate")        # keepalive, as clients do
            n = len(sink.packets)
            self.assertGreaterEqual(n, 18)             # ~25 fps
            self.assertLessEqual(n, 40)
            _, d, _ = sink.packets[3]
            self.assertEqual(struct.unpack("<H", d[0:2])[0], len(d) & 0xFFFF)
            self.assertEqual(d[9:15].hex().upper(), "024C58E00001")
            self.assertEqual(d[15:17], b"\x00\x21")
            start = struct.unpack(">H", d[30:32])[0] + 32
            self.assertEqual(start, 254)
            self.assertEqual(d[start:start + 2], b"\xff\xd8")
            self.assertEqual(d[-2:], b"\xff\xd9")
            self.assertEqual(jpeg_size(d[start:]), (640, 360))
            self.assertTrue(22000 < len(d) < 32000, len(d))
            self.assertEqual(await self.result(mode="stopstream"), "ok")
            await asyncio.sleep(0.2)
            n = len(sink.packets)
            await asyncio.sleep(0.3)
            self.assertEqual(len(sink.packets), n)
        finally:
            tr.close()

    async def test_no_frames_before_subscribe(self):
        """The S5 sits on "Connecting" until a SUBSCRIBE: startstream ok, 0 datagrams."""
        tr, sink, port = await self._sink()
        try:
            await self.connect()
            self.assertEqual(await self.result(mode="camcmd", value="recmode"), "ok")
            await asyncio.sleep(0.1)
            self.assertEqual(await self.result(mode="startstream", value=str(port)), "ok")
            await asyncio.sleep(0.5)
            self.assertEqual(sink.packets, [])
        finally:
            tr.close()

    async def test_mode_switch_to_play_stops_stream(self):
        tr, sink, port = await self._sink()
        try:
            await self.ready_in_rec()
            await self.result(mode="startstream", value=str(port))
            await asyncio.sleep(0.3)
            self.assertTrue(sink.packets)
            await self.control("body/play")
            await asyncio.sleep(0.2)
            n = len(sink.packets)
            await asyncio.sleep(0.3)
            self.assertEqual(len(sink.packets), n)
            self.assertEqual(await self.result(mode="stopstream"), "ok")
        finally:
            tr.close()


class FilesTest(EmuTestBase):
    async def browse(self, oid="0", start=0, count=50):
        body = (
            '<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/'
            'soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            '<u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1" xmlns:pana="urn:'
            'schemas-panasonic-com:pana"><ObjectID>%s</ObjectID><BrowseFlag>BrowseDirectChildren'
            '</BrowseFlag><Filter>*</Filter><StartingIndex>%d</StartingIndex><RequestedCount>%d'
            '</RequestedCount><SortCriteria></SortCriteria><pana:X_FromCP>LumixLink2.0'
            '</pana:X_FromCP></u:Browse></s:Body></s:Envelope>' % (oid, start, count)).encode()
        url = "http://127.0.0.1:%d/Server0/CDS_control" % self.port("upnp")
        async with self.http.post(url, data=body, headers={
                "SOAPACTION": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
                "Content-Type": 'text/xml; charset="utf-8"'}) as r:
            raw = await r.read()
        root = ET.fromstring(raw)
        vals = {el.tag.split("}")[-1]: el.text for el in root.iter()}
        if "errorCode" in vals:
            return int(vals["errorCode"]), None, None
        didl = ET.fromstring(vals["Result"])
        return int(vals["NumberReturned"]), int(vals["TotalMatches"]), didl

    async def test_browse_needs_play_and_pages(self):
        await self.ready_in_rec()
        code, _, _ = await self.browse()
        self.assertEqual(code, 701)                    # rec mode
        self.assertEqual(await self.result(mode="camcmd", value="playmode"), "ok")
        await asyncio.sleep(0.3)
        ci = await self.cgi(mode="get_content_info")
        self.assertIn("<total_content_number>126</total_content_number>", ci)
        n, total, didl = await self.browse()
        ns = "{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}"
        conts = didl.findall(ns + "container")
        items = didl.findall(ns + "item")
        self.assertEqual(len(conts), 1)
        self.assertEqual(n, total)
        self.assertEqual(len(items) + 1, total)
        g = conts[0]
        self.assertTrue(g.get("id").endswith("DIR"))
        self.assertEqual(g.get("childCount"), "120")
        self.assertEqual(g.find("{urn:schemas-panasonic-com:pana}X_RecGroupType").text, "Interval")
        kinds = [it.find("{urn:schemas-upnp-org:metadata-1-0/upnp/}class").text for it in items]
        self.assertIn("object.item.videoItem.movie", kinds)
        self.assertIn("object.item.imageItem", kinds)
        got = []
        for start in (0, 50, 100):
            n, total, page = await self.browse(g.get("id"), start, 50)
            self.assertEqual(total, 120)
            got += [it.get("id") for it in page.findall(ns + "item")]
        self.assertEqual(len(got), 120)
        self.assertEqual(len(set(got)), 120)
        code, _, _ = await self.browse("19999999DIR")
        self.assertEqual(code, 701)

    async def test_range_download(self):
        await self.connect()
        n, total, didl = await self.browse()
        ns = "{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}"
        item = didl.findall(ns + "item")[0]
        res = item.findall(ns + "res")
        org = res[0]
        url = org.text
        self.assertTrue(url.startswith("http://127.0.0.1:%d/DO" % self.port("dl")), url)
        size = int(org.get("size"))
        async with self.http.get(url) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers["Accept-Ranges"], "bytes")
            full = await r.read()
        self.assertEqual(len(full), size)
        self.assertEqual(full[:2], b"\xff\xd8")
        async with self.http.get(url, headers={"Range": "bytes=100-199"}) as r:
            self.assertEqual(r.status, 206)
            self.assertEqual(r.headers["Content-Range"], "bytes 100-199/%d" % size)
            self.assertEqual(await r.read(), full[100:200])
        async with self.http.get(url, headers={"Range": "bytes=%d-" % (size - 10)}) as r:
            self.assertEqual(await r.read(), full[-10:])
        async with self.http.get(url, headers={"Range": "bytes=-5"}) as r:
            self.assertEqual(await r.read(), full[-5:])
        async with self.http.get(url, headers={"Range": "bytes=%d-" % size}) as r:
            self.assertEqual(r.status, 416)
        thumb = [x for x in res if "/DT" in x.text][0]
        self.assertEqual(thumb.get("size"), "5000")     # the S5's fake size attribute
        async with self.http.get(thumb.text) as r:
            data = await r.read()
        self.assertEqual(jpeg_size(data), (160, 120))
        # files are only served in play mode
        await self.subscribe()
        await self.events.wait_for("mod_Play")
        await self.result(mode="camcmd", value="recmode")
        await asyncio.sleep(0.1)
        async with self.http.get(url) as r:
            self.assertEqual(r.status, 404)


class IntervalTest(EmuTestBase):
    cfg_kw = {"interval_scale": 0.002}

    async def test_interval_run(self):
        await self.ready_in_rec()
        # the recorded body boots in Time Lapse drive (curmenu); start from Single
        self.assertEqual(await self.result(mode="setsetting", type="drivemode", value="normal"), "ok")
        self.assertEqual(await self.result(mode="camctrl", type="interval", value="set"),
                         "err_reject")                  # drive mode is not interval yet
        self.assertEqual(await self.result(mode="setsetting", type="drivemode",
                                           value="stopmotion_interval"), "ok")
        self.assertIn("<burst_interval_status>on</burst_interval_status>",
                      await self.cgi(mode="getstate"))
        self.assertEqual(await self.result(mode="camctrl", type="interval", value="set"),
                         "ok_under_research")
        self.assertEqual(await self.result(mode="camctrl", type="interval", value="set"), "ok")
        before = (await self.control("state"))["card"]
        self.assertEqual(await self.result(mode="camcmd", value="capture"), "ok")
        await asyncio.sleep(0.3)
        st = await self.cgi(mode="getstate")
        self.assertIn("<interval_status>run</interval_status>", st)
        self.assertEqual(await self.result(mode="camcmd", value="recmode"), "err_busy")
        self.assertEqual(await self.result(mode="camctrl", type="interval", value="cancel"), "ok")
        after = (await self.control("state"))["card"]
        self.assertGreater(after["total"], before["total"])
        self.assertEqual(after["top_level"], before["top_level"] + 1)   # one new Interval group
        self.assertIn("<warn_disp>interval_canceled</warn_disp>", await self.cgi(mode="getstate"))


class MultiCameraTest(unittest.IsolatedAsyncioTestCase):
    async def test_two_independent_cameras(self):
        a = await EmulatorNode("127.0.0.1", 0, 0, 0, 0, Identity.for_index(0), fast_cfg()).start()
        b = await EmulatorNode("127.0.0.1", 0, 0, 0, 0, Identity.for_index(1), fast_cfg()).start()
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True)) as s:
                names = []
                for n in (a, b):
                    async with s.get("http://127.0.0.1:%d/Lumix/Server0/ddd" % n.ports["upnp"]) as r:
                        names.append(re.search("<friendlyName>([^<]+)", await r.text()).group(1))
                self.assertEqual(names, ["S5-E00001", "S5-E00002"])
                async with s.post("http://127.0.0.1:%d/emu/wedge" % a.ports["control"]) as r:
                    self.assertTrue((await r.json())["wedged"])
                async with s.get("http://127.0.0.1:%d/emu/state" % b.ports["control"]) as r:
                    self.assertFalse((await r.json())["wedged"])
        finally:
            await a.stop()
            await b.stop()


if __name__ == "__main__":
    unittest.main()
