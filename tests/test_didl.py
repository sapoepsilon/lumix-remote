import os
import unittest

from lumix import didl

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _read(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


FAULT = b"""<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>
<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>
<UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>701</errorCode>
<errorDescription>No such object</errorDescription></UPnPError></detail></s:Fault></s:Body></s:Envelope>"""


class BrowseEnvelopeTest(unittest.TestCase):
    def test_counts_and_result(self):
        r = didl.parse_browse_response(_read("cds_browse_synthetic.xml"))
        self.assertEqual((r.returned, r.total), (3, 7))
        self.assertTrue(r.didl.lstrip().startswith("<DIDL-Lite"))

    def test_fault_raises_with_code(self):
        with self.assertRaises(didl.BrowseError) as cm:
            didl.parse_browse_response(FAULT)
        self.assertEqual(cm.exception.code, "701")

    def test_request_body(self):
        b = didl.browse_body("0", 50, 25).decode()
        self.assertIn("<StartingIndex>50</StartingIndex>", b)
        self.assertIn("<RequestedCount>25</RequestedCount>", b)
        self.assertIn("<pana:X_FromCP>LumixLink2.0</pana:X_FromCP>", b)


class SyntheticDidlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = didl.parse_browse_response(_read("cds_browse_synthetic.xml"))
        cls.items, cls.conts = didl.parse_didl(r.didl)

    def test_containers(self):
        self.assertEqual([(c.object_id, c.child_count) for c in self.conts], [("01011980DIR", 3)])

    def test_raw_plus_jpeg_photo(self):
        it = self.items[0]
        self.assertEqual(it.kind, "photo")
        self.assertEqual(it.name, "P1010001.JPG")
        self.assertEqual(it.original.name, "DO1010001.JPG")
        self.assertEqual(it.raw.name, "DO1010001.RW2")
        self.assertEqual(it.raw_name, "P1010001.RW2")
        self.assertEqual(it.thumb.name, "DT1010001.JPG")
        self.assertEqual(it.preview.name, "DL1010001.JPG")
        self.assertEqual(it.size, 9876543)
        self.assertEqual(it.date_folder(), "2020-02-26")
        d = it.to_dict()
        self.assertEqual(d, {"id": "1010001", "name": "P1010001.JPG", "kind": "photo",
                             "date": "2020-02-26T09:10:11", "size": 9876543, "has_raw": True})

    def test_video(self):
        it = self.items[1]
        self.assertEqual(it.kind, "video")
        self.assertEqual(it.name, "P1010002.MP4")
        self.assertIsNone(it.raw)
        self.assertEqual(it.preview.name, "DT1010002.JPG")  # falls back to thumbnail
        self.assertEqual(it.original.duration, "0:00:12")

    def test_raw_only_item(self):
        x = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
             'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
             '<item id="a/b" parentID="0"><dc:title>P1000009</dc:title><upnp:class>object.item.imageItem</upnp:class>'
             '<res protocolInfo="http-get:*:application/octet-stream:*" size="10">http://x:50001/DO1000009.RW2</res>'
             '</item></DIDL-Lite>')
        (it,), _ = didl.parse_didl(x)
        self.assertEqual(it.name, "P1000009.RW2")
        self.assertEqual(it.original.name, "DO1000009.RW2")
        self.assertIsNone(it.raw)            # the RAW *is* the original
        self.assertEqual(len(it.id), 16)     # unsafe object id -> hashed

    def test_empty(self):
        self.assertEqual(didl.parse_didl(""), ([], []))

    def test_dates(self):
        self.assertEqual(str(didl.parse_date("2020-02-26T09:52:01")), "2020-02-26 09:52:01")
        self.assertEqual(str(didl.parse_date("2020/02/26 09:52")), "2020-02-26 09:52:00")
        self.assertIsNone(didl.parse_date("junk"))


if __name__ == "__main__":
    unittest.main()


class RealS5Test(unittest.TestCase):
    """Browse replies captured from a DC-S5 (fw 2.03) on 2026-10-11."""

    def test_root_groups_and_single_files(self):
        r = didl.parse_browse_response(_read("s5_cds_root.xml"))
        self.assertEqual((r.returned, r.total), (12, 12))
        items, conts = didl.parse_didl(r.didl)
        self.assertEqual(len(conts), 9)
        c = conts[0]
        self.assertEqual((c.object_id, c.title, c.child_count, c.group_type),
                         ("11024091DIR", "102-4091", 960, "Interval"))
        self.assertTrue(c.thumb_url.endswith("/DT11024091.JPG"))
        by = {i.object_id: i for i in items}
        mp4, mov, jpg = by["11067148"], by["11090030"], by["11090991"]
        self.assertEqual((mp4.name, mp4.kind, mp4.size), ("P1067148.MP4", "video", 44437838))
        self.assertEqual((mov.name, mov.kind, mov.date), ("P1090030.MOV", "video", None))
        self.assertEqual(mov.date_folder(), "undated")
        self.assertEqual((jpg.name, jpg.kind, jpg.date), ("P1090991.JPG", "photo", "2020-02-25T15:13:25"))
        self.assertEqual((jpg.thumb.name, jpg.preview.name), ("DT11090991.JPG", "DL11090991.JPG"))
        self.assertFalse(jpg.to_dict()["has_raw"])

    def test_group_children(self):
        r = didl.parse_browse_response(_read("s5_cds_interval_group.xml"))
        self.assertEqual((r.returned, r.total), (2, 960))
        items, conts = didl.parse_didl(r.didl)
        self.assertEqual(conts, [])
        self.assertEqual([(i.id, i.name, i.parent_id, i.size) for i in items],
                         [("11024091", "P1024091.JPG", "11024091DIR", 6471168),
                          ("11024092", "P1024092.JPG", "11024091DIR", 7464448)])
        self.assertEqual(items[0].date_folder(), "2020-02-10")
