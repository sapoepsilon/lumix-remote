"""cameras.json registry, migration of the single-camera files, pidfile unification."""
import isolate  # noqa: F401  (temp config dir; must come first)
import json
import os
import tempfile
import unittest
from unittest import mock

from lumix import ble, registry


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LUMIX_REMOTE_HOME": self.tmp.name})
        self.env.start()
        self.reg = registry.Registry()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_add_ids_ports_active(self):
        a = self.reg.add("S5 A-cam")
        b = self.reg.add("S5 A-cam")
        self.assertEqual((a["id"], b["id"]), ("s5-a-cam", "s5-a-cam-2"))
        self.assertEqual((a["ports"]["liveview"], b["ports"]["liveview"]), (49199, 49200))
        self.assertEqual(a["ports"]["http"], 80)
        self.assertEqual(self.reg.active_id(), "s5-a-cam")
        self.assertTrue(a["client_uuid"].startswith("4D454930-0100-1000-8001-"))
        self.assertEqual(self.reg.load()["version"], 1)

    def test_update_merges_nested(self):
        a = self.reg.add("cam", ports={"http": 8080})
        e = self.reg.update(a["id"], ports={"upnp": 1234}, settings={"watchdog": True})
        self.assertEqual((e["ports"]["http"], e["ports"]["upnp"], e["ports"]["files"]), (8080, 1234, 50001))
        self.assertEqual(e["settings"], {"autoconnect": True, "watchdog": True})

    def test_find_rename_remove_use(self):
        a, b = self.reg.add("One"), self.reg.add("Two")
        self.assertEqual(self.reg.find("two")["id"], b["id"])
        self.reg.rename(b["id"], "B-cam")
        self.assertEqual(self.reg.find("b-cam")["id"], b["id"])
        self.reg.set_active(b["id"])
        self.assertEqual(self.reg.active_id(), b["id"])
        os.makedirs(self.reg.camera_dir(b["id"]))
        self.reg.remove(b["id"])
        self.assertEqual(self.reg.active_id(), a["id"])
        self.assertFalse(os.path.exists(self.reg.camera_dir(b["id"])))
        with self.assertRaises(registry.RegistryError):
            self.reg.set_active("nope")
        with self.assertRaises(registry.RegistryError):
            self.reg.add("x", cam_id="Bad Id")

    def test_broken_file_is_kept(self):
        with open(self.reg.path, "w") as f:
            f.write("{nope")
        self.assertEqual(self.reg.cameras(), [])
        self.assertTrue(os.path.exists(self.reg.path + ".broken"))

    def test_registry_store(self):
        a = self.reg.add("cam")
        st = registry.RegistryStore(self.reg, a["id"])
        st.save(last_ip="10.0.0.9", udn="uuid:x", bogus=1)
        self.assertEqual(st.load()["last_ip"], "10.0.0.9")
        self.assertNotIn("bogus", self.reg.get(a["id"]))
        self.reg.remove(a["id"])
        st.save(last_ip="10.0.0.1")       # camera gone: ignored, no error


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.env = mock.patch.dict(os.environ, {"LUMIX_REMOTE_HOME": self.d})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, name, data):
        with open(os.path.join(self.d, name), "w") as f:
            json.dump(data, f)

    def test_migrates_state_ble_presets(self):
        self.write("state.json", {"last_ip": "10.1.2.3"})
        self.write("ble.json", {"camera_name": "S5-ABC123", "home_ssid": "Home", "registered_at": 5})
        self.write("presets.json", {"auto": {"on": True, "saved": {"iso": ["400", None]}}})
        rep = registry.migrate()
        self.assertEqual(rep["camera"], "s5")
        reg = registry.Registry()
        e = reg.get("s5")
        self.assertEqual(e["nickname"], "S5")
        self.assertEqual(e["last_ip"], "10.1.2.3")
        self.assertFalse(e["provisional"])
        self.assertEqual((e["ble"]["name"], e["ble"]["home_ssid"], e["ble"]["registered_at"]),
                         ("S5-ABC123", "Home", 5))
        with open(os.path.join(reg.camera_dir("s5"), "presets.json")) as f:
            self.assertTrue(json.load(f)["auto"]["on"])
        for n in ("state.json", "ble.json", "presets.json"):
            self.assertFalse(os.path.exists(os.path.join(self.d, n)))
            self.assertTrue(os.path.exists(os.path.join(self.d, "legacy", n)))
        self.assertIsNone(registry.migrate())          # idempotent

    def test_nothing_to_migrate(self):
        self.assertIsNone(registry.migrate())
        self.assertFalse(os.path.exists(os.path.join(self.d, "cameras.json")))

    def test_ble_helpers_use_registry(self):
        self.write("ble.json", {"camera_name": "S5-ABC123", "home_ssid": "Home"})
        self.assertEqual(ble.load_config()["home_ssid"], "Home")       # migrates on first use
        reg = registry.Registry()
        cid = reg.add("Other")["id"]
        with mock.patch.dict(os.environ, {"LUMIX_CAMERA_ID": cid}):
            ble.save_config(camera_name="G9-000001", registered_at=7)
            self.assertEqual(ble.load_config()["camera_name"], "G9-000001")
        self.assertEqual(reg.get(cid)["ble"]["registered_at"], 7)
        self.assertEqual(ble.load_config()["camera_name"], "S5-ABC123")  # active camera unchanged

    def test_legacy_pidfile(self):
        old = os.path.join(self.d, "old.pid")
        with open(old, "w") as f:
            f.write("123\n")
        self.assertEqual(registry.migrate_pidfile([old]), old)
        with open(registry.pidfile_path()) as f:
            self.assertEqual(f.read().strip(), "123")
        self.assertFalse(os.path.exists(old))


if __name__ == "__main__":
    unittest.main()
