"""FileBrowser paging over a fake ContentDirectory (no camera needed)."""
import isolate  # noqa: F401  (temp config dir; must come first)
import asyncio
import unittest

from lumix import didl
from lumix.files import FileBrowser

DIDL = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:pana="urn:schemas-panasonic-com:pana">%s</DIDL-Lite>')


def item(n, ext="JPG", parent="0"):
    return ('<item id="%d" parentID="%s"><dc:title>%s-%s</dc:title>'
            '<upnp:class>object.item.%s</upnp:class>'
            '<res protocolInfo="http-get:*:%s:*" size="10">http://c:50001/DO%d.%s</res>'
            '<res protocolInfo="http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_TN">http://c:50001/DT%d.JPG</res>'
            '</item>' % (n, parent, str(n)[1:4], str(n)[4:], "videoItem" if ext == "MP4" else "imageItem",
                         "video/mp4" if ext == "MP4" else "image/jpeg", n, ext, n))


# card: group 11000001DIR (5 stills 11000001..5), then video 11000006, then still 11000007
ROOT = [('<container id="11000001DIR" parentID="0" childCount="5"><dc:title>100-0001</dc:title>'
         '<pana:X_RecGroupType>Interval</pana:X_RecGroupType></container>'),
        item(11000006, "MP4"), item(11000007)]
GROUP = [item(11000001 + i, parent="11000001DIR") for i in range(5)]


class FakeCam:
    ip = "127.0.0.1"
    state = {"cammode": "play"}
    wedged = False
    body_busy = False


class PagingTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def make(self):
        fb = FileBrowser(FakeCam(), page=2)

        async def ensure_play():
            fb.active = True

        async def browse(oid, start, count):
            self.calls.append((oid, start, count))
            src = ROOT if oid == "0" else GROUP
            part = src[start:start + count]
            return didl.BrowseResult(didl=DIDL % "".join(part), returned=len(part), total=len(src))

        fb.ensure_play = ensure_play
        fb._browse = browse
        self.fb = fb

    def names(self, res):
        return [i["name"] for i in res["items"]]

    def test_newest_first_and_lazy_group(self):
        async def go():
            self.make()
            first = await self.fb.list(0, 2)
            self.assertEqual(first["total"], 7)
            self.assertEqual(self.names(first), ["P1000007.JPG", "P1000006.MP4"])
            self.assertFalse(any(c[0] != "0" for c in self.calls))   # group not expanded yet
            rest = await self.fb.list(2, 10)
            self.assertEqual(self.names(rest), ["P1000005.JPG", "P1000004.JPG", "P1000003.JPG",
                                                "P1000002.JPG", "P1000001.JPG"])
            mid = await self.fb.list(3, 2)
            self.assertEqual(self.names(mid), ["P1000004.JPG", "P1000003.JPG"])
            vids = await self.fb.list(0, 10, kind="video")
            self.assertEqual((vids["total"], self.names(vids)), (1, ["P1000006.MP4"]))
            photos = await self.fb.list(0, 3, kind="photo")
            self.assertEqual((photos["total"], self.names(photos)),
                             (6, ["P1000007.JPG", "P1000005.JPG", "P1000004.JPG"]))
            it = photos["items"][1]
            self.assertEqual(it["thumb_url"], "/api/files/11000005/thumb")
            self.assertNotIn("raw_url", it)
        asyncio.run(go())

    def test_get_item_by_id_without_listing_the_group(self):
        async def go():
            self.make()
            it = await self.fb.get_item("11000003")
            self.assertEqual(it.name, "P1000003.JPG")
            self.assertEqual([c for c in self.calls if c[0] != "0"],
                             [("11000001DIR", 0, 2), ("11000001DIR", 2, 2), ("11000001DIR", 4, 1)])
            with self.assertRaises(Exception):
                await self.fb.get_item("10999999")   # older than every group
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
