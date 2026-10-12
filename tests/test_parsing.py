import isolate  # noqa: F401  (temp config dir; must come first)
import os
import struct
import unittest

from lumix import exposure
from lumix.liveview import extract_jpeg
from lumix.menu import MenuModel
from lumix.protocol import build_query, parse_reply

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _read(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


class MenuTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = MenuModel(_read("s5_allmenu.xml"))
        cls.m.apply_curmenu(_read("s5_curmenu.xml"))

    def test_quick_settings_found_by_cmd_type(self):
        for t, sid in [("iso", "menu_item_id_sensitivity"), ("exposure", "menu_item_id_exposure3"),
                       ("whitebalance", "menu_item_id_whitebalance"), ("focusmode", "menu_item_id_focusmode"),
                       ("videoquality", "menu_item_id_v_quality")]:
            self.assertEqual(self.m.by_cmd_type(t).id, sid)

    def test_current_values_and_titles(self):
        s = self.m.settings["menu_item_id_exposure3"]
        self.assertEqual(s.current, "1/3")
        self.assertEqual(s.current_option().title, "+1/3")
        self.assertEqual(self.m.settings["menu_item_id_v_quality"].current_option().title,
                         "3840x2160,24p,422/10bit")

    def test_placeholders_removed(self):
        opt = self.m.settings["menu_item_id_ph_sty"].options[0]
        self.assertEqual(opt.value, "standard")
        self.assertIsNone(opt.value2)

    def test_submenu_path(self):
        self.assertEqual(self.m.settings["menu_item_id_bracket_type"].path, ["Bracketing"])


class ExposureTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(exposure.aperture_label(939), "F3.5")
        self.assertEqual(exposure.aperture_label(2304), "F22")
        self.assertEqual(exposure.shutter_label(3328), "1/8000")
        self.assertEqual(exposure.shutter_label(-1536), '60"')
        self.assertEqual(exposure.shutter_label(0), '1"')

    def test_lens_options(self):
        lens = exposure.parse_lens("ok,2304/256,939/256,3328/256,-1536/256,0,off,60,20,on,128/1024,on,off,l,"
                                   "LUMIX S 20-60/F3.5-5.6,Panasonic,1653,256".split(","))
        self.assertEqual(lens["name"], "LUMIX S 20-60/F3.5-5.6")
        ap = exposure.aperture_options(lens)
        self.assertEqual((ap[0]["title"], ap[-1]["title"], len(ap)), ("F3.5", "F22", 17))
        ss = exposure.shutter_options(lens)
        self.assertEqual((ss[0]["title"], ss[-1]["title"]), ("1/8000", '60"'))


class ProtocolTest(unittest.TestCase):
    def test_query_keeps_slashes(self):
        self.assertEqual(build_query("setsetting", "exposure", "-1/3"),
                         "mode=setsetting&type=exposure&value=-1/3")
        self.assertIn("value2=SM%20G", build_query("accctrl", "req_acc", "x", "SM G"))

    def test_parse_xml_and_csv(self):
        r = parse_reply("q", 200, b'<?xml version="1.0"?><camrply><result>err_busy</result></camrply>', 0)
        self.assertEqual(r.result, "err_busy")
        r = parse_reply("q", 200, b"ok,S5-XXXXXX,remote,encrypted", 0)
        self.assertTrue(r.ok)
        self.assertEqual(r.csv[1], "S5-XXXXXX")

    def test_extract_jpeg_header_offset(self):
        jpeg = b"\xff\xd8\xff\xe0" + b"x" * 100 + b"\xff\xd9"
        hdr = bytearray(254)
        hdr[30:32] = struct.pack(">H", 254 - 32)
        self.assertEqual(extract_jpeg(bytes(hdr) + jpeg), jpeg)


if __name__ == "__main__":
    unittest.main()
