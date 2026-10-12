"""Live view quality (setsetting liveviewsize) and the frame endpoints.

Real evidence: tests/fixtures/real-s5-liveview-quality-2026-10-12.json (DC-S5
fw 2.03): the body offers vga (640x360) and qvga (320x184), rejects hd with
err_param, applies a new size only at the next startstream, and sends one UDP
datagram per frame at both sizes. The emulator reproduces that; the server
tests run the real app against it.
"""
import isolate  # noqa: F401  (temp config dir; must come first)

import json
import os
import struct
import unittest

from lumix import models
from lumix.camera import Camera, parse_lv_quality_options
from lumix.liveview import extract_jpeg, jpeg_size
from test_timelapse_emu import EmuTestCase

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load_real():
    with open(os.path.join(FIX, "real-s5-liveview-quality-2026-10-12.json")) as f:
        return json.load(f)


def tiny_jpeg(w, h):
    """SOI + APP0-ish filler + SOF0 (w x h) + EOI: enough for the parsers."""
    app = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app + sof + b"\x00" * 32 + b"\xff\xd9"


class RealEvidenceTest(unittest.TestCase):
    def test_curmenu_offers_vga_and_qvga(self):
        with open(os.path.join(FIX, "s5_curmenu.xml")) as f:
            self.assertEqual(parse_lv_quality_options(f.read()), ["vga", "qvga"])
        self.assertEqual(parse_lv_quality_options("\n".join(load_real()["curmenu_items"])), ["vga", "qvga"])

    def test_s5_profile_default_is_the_best_size(self):
        self.assertEqual(models.S5.liveview.default_quality, "vga")
        self.assertEqual(models.S5.liveview.qualities, ("vga", "qvga"))

    def test_hd_is_rejected_and_sizes_measured(self):
        fx = load_real()
        hd = [c for c in fx["commands"] if c[1].endswith("value=hd")]
        self.assertEqual(hd[0][2], "err_param")
        runs = fx["runs"]
        for name, r in runs.items():
            want = "(640, 360)" if name.startswith("vga") else "(320, 184)"
            self.assertEqual(list(r["mjpeg"]["res"]), [want], name)
        # one datagram per frame, nothing unparsable, IP reassembly 1:1 (no multi-packet frames)
        for name in ("vga_a", "vga_b", "vga_long", "qvga_a", "qvga_b"):
            r = runs[name]
            self.assertEqual(r["rx_datagrams"], r["frames"], name)
            self.assertEqual(r["rx_bad"], 0, name)
            self.assertLessEqual(abs(r["ip_reassembled"] - r["frames"]), 2, name)
        # qvga buys < 30 % more frames for a quarter of the pixels: vga stays the default
        fps = lambda n: runs[n]["frames"] / runs[n]["mjpeg"]["secs"]
        vga = (fps("vga_a") + fps("vga_b")) / 2
        qvga = (fps("qvga_a") + fps("qvga_b")) / 2
        self.assertGreater(qvga, vga)
        self.assertLess(qvga / vga, 1.3)
        self.assertGreater(fps("vga_long"), 15)

    def test_real_header_parses_and_length_is_be16(self):
        fx = load_real()
        hdr = bytes.fromhex(fx["vga_header_hex_scrubbed"])
        self.assertEqual(len(hdr), 254)
        self.assertEqual(struct.unpack(">H", hdr[0:2])[0], fx["vga_datagram_bytes"])
        self.assertEqual(hdr[15:17], b"\x00\x21")                 # packet type 33 = image
        self.assertEqual(hdr[9:15].hex(), "024c58e00001")         # scrubbed MAC
        for w, h in ((640, 360), (320, 184)):
            j = tiny_jpeg(w, h)
            d = hdr + j
            self.assertEqual(extract_jpeg(d, models.S5.liveview), j)
            self.assertEqual(jpeg_size(extract_jpeg(d, models.S5.liveview)), (w, h))

    def test_jpeg_size_edge_cases(self):
        self.assertIsNone(jpeg_size(b"\xff\xd8\xff\xd9"))
        self.assertIsNone(jpeg_size(b""))
        self.assertEqual(jpeg_size(tiny_jpeg(1920, 1080)), (1920, 1080))


class QualityServerTest(EmuTestCase):
    def setUp(self):
        self._gap = Camera.LV_QUALITY_GAP_S
        Camera.LV_QUALITY_GAP_S = 0.1
        super().setUp()

    def tearDown(self):
        super().tearDown()
        Camera.LV_QUALITY_GAP_S = self._gap

    def cgi_log(self):
        return [e["q"] for e in self.rig.emu.log if e["kind"] == "cgi"]

    def test_switch_restarts_stream_and_persists(self):
        r = self.rig

        async def go():
            cam = r.sess.cam
            await r.wait(lambda: cam.liveview.size == (640, 360), 10, "vga frames")
            st, d = await r.call("GET", "/api/cameras/emu/status")
            self.assertEqual(d["liveview_quality"]["current"], "vga")
            self.assertEqual(d["liveview_quality"]["options"], ["vga", "qvga"])
            self.assertEqual(d["liveview"]["width"], 640)

            st, d = await r.call("POST", "/api/cameras/emu/liveview/quality", {"liveview_quality": "hd"})
            self.assertEqual(st, 400, d)

            n0 = len(self.cgi_log())
            st, d = await r.call("POST", "/api/cameras/emu/liveview/quality", {"liveview_quality": "qvga"})
            self.assertEqual(st, 200, d)
            self.assertTrue(d["applied"])
            self.assertTrue(d["restarted"])
            self.assertEqual(d["liveview_quality"]["wanted"], "qvga")
            await r.wait(lambda: cam.liveview.size == (320, 184), 10, "qvga frames")
            sent = [q for q in self.cgi_log()[n0:] if "getstate" not in q and "getsetting" not in q
                    and "getinfo" not in q]
            self.assertEqual([q.split("&value=")[0] if "startstream" in q else q for q in sent][:3],
                             ["mode=setsetting&type=liveviewsize&value=qvga", "mode=stopstream",
                              "mode=startstream"])
            self.assertFalse(any("recmode" in q for q in sent))   # already in rec: no mode switch
            self.assertEqual(r.mgr.registry.get("emu").get("liveview_quality"), "qvga")

            # latest frame: ?after=<seq> never returns the same frame twice
            resp = await r.client.get("/api/cameras/emu/liveview.jpg")
            self.assertEqual(resp.status, 200)
            seq = int(resp.headers["X-Frame-Seq"])
            self.assertEqual(resp.headers["X-Frame-Width"], "320")
            self.assertEqual(jpeg_size(await resp.read()), (320, 184))
            resp = await r.client.get("/api/cameras/emu/liveview.jpg?after=%d" % seq)
            self.assertGreater(int(resp.headers["X-Frame-Seq"]), seq)

            # MJPEG: multipart JPEG parts
            resp = await r.client.get("/api/cameras/emu/liveview.mjpg")
            self.assertIn("multipart/x-mixed-replace", resp.headers["Content-Type"])
            head = await resp.content.readuntil(b"\r\n\r\n")
            self.assertIn(b"Content-Type: image/jpeg", head)
            n = int([l for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")][0].split(b":")[1])
            self.assertEqual(jpeg_size(await resp.content.readexactly(n)), (320, 184))
            resp.close()

            st, d = await r.call("GET", "/api/cameras/emu/liveview/stats")
            self.assertGreater(d["datagrams"], 0)
            self.assertEqual(d["bad"], 0)
            self.assertEqual(d["header_bytes"], 254)

        self.run_(go())

    def test_connect_applies_saved_quality_before_startstream(self):
        r = self.rig

        async def go():
            st, d = await r.call("POST", "/api/cameras/emu/liveview/quality", {"liveview_quality": "qvga"})
            self.assertEqual(st, 200, d)
            await r.stop_server()
            r.emu.settings["liveviewsize"] = ("vga", "")      # e.g. changed by another app
            n0 = len(self.cgi_log())
            await r.start_server()
            await r.wait(lambda: r.sess.cam.liveview.size == (320, 184), 10, "qvga frames after connect")
            sent = self.cgi_log()[n0:]
            i_set = sent.index("mode=setsetting&type=liveviewsize&value=qvga")
            i_start = max(i for i, q in enumerate(sent) if q.startswith("mode=startstream"))
            self.assertLess(i_set, i_start)

        self.run_(go())


if __name__ == "__main__":
    unittest.main()
