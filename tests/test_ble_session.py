"""BLE session logic against a stub camera (no radio, no bleak).

The stub behaves like the real DC-S5 did on 2026-10-12 with the power switch
OFF and Remote Wakeup ON (tests/fixtures/real-s5-ble-wake-2026-10-12.log):

- an unknown client id is cut off right after the friendly-name write (run A);
- the paired id logs in, the 0d6f1880=03 write is acked on 18345be1;
- Wi-Fi only comes up after the LUMIX Sync Remote-shooting sequence
  SSID, 03, ack, 02, start 03 (run C); SSID, 03, start 01 never got the woken
  camera onto the network (run B).
"""
import isolate  # noqa: F401  (temp config dir; must come first)

import asyncio
import os
import re
import types
import unittest
from unittest import mock

from lumix import ble, camera, registry

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                       "real-s5-ble-wake-2026-10-12.log")
PAIRED = "4D454930-0100-1000-8001-0A0B0C0D0E0F"


class StubCamera:
    """GATT behaviour of the S5 as seen from bleak."""

    def __init__(self, known_id=PAIRED, connect_fails=0, ack=b"\x00", wifi_needs=(3, 2)):
        self.known = bytes.fromhex(known_id.replace("-", ""))
        self.connect_fails = connect_fails
        self.ack = ack
        self.wifi_needs = list(wifi_needs)      # 0d6f1880 values a join needs, in order
        self.writes = []                        # (short uuid, hex)
        self.reads = []
        self.wifi_up = False


def fake_bleak(cam: StubCamera):
    class Client:
        def __init__(self, dev, disconnected_callback=None, timeout=10):
            self.timeout = timeout
            self._disc = disconnected_callback
            self.is_connected = False
            self._cbs = {}
            self._id_ok = None

        async def connect(self):
            if cam.connect_fails:
                cam.connect_fails -= 1
                raise asyncio.TimeoutError()
            self.is_connected = True

        async def disconnect(self):
            self.is_connected = False

        def _drop(self):
            self.is_connected = False
            if self._disc:
                self._disc(self)

        async def start_notify(self, uuid, cb):
            # macOS finds no CCCDs on the S5, but the camera notifies anyway
            self._cbs[uuid] = cb
            raise RuntimeError("attribute not found")

        def _notify(self, uuid, data):
            for u, cb in self._cbs.items():
                if u == uuid:
                    cb(None, bytearray(data))

        async def write_gatt_char(self, uuid, data, response=True):
            if not self.is_connected:
                raise RuntimeError("disconnected")
            data = bytes(data)
            cam.writes.append((uuid[:8], data.hex()))
            if uuid == ble.CH_DLNA_UUID:
                self._id_ok = data == cam.known
            elif uuid == ble.CH_FRIENDLY_NAME and not self._id_ok:
                self._drop()                    # run A: HCI 0x13 right after the name write
                raise RuntimeError("disconnected")
            elif uuid == ble.CH_WIFI_CMD:
                if data == bytes([ble.WIFI_STA_SSID_SET]):
                    asyncio.get_running_loop().call_later(0.01, self._notify, ble.CH_WIFI_CMD_ACK, cam.ack)
            elif uuid == ble.CH_WIFI_START:
                cmds = [int(h, 16) for u, h in cam.writes if u == ble.CH_WIFI_CMD[:8]]
                cam.wifi_up = cmds[-len(cam.wifi_needs):] == cam.wifi_needs and data == b"\x03"

        async def read_gatt_char(self, uuid):
            if not self.is_connected or not self._id_ok:
                self._drop()
                raise RuntimeError("disconnected")
            cam.reads.append(uuid[:8])
            return {ble.CH_INITIAL_INFORM: b"\xc0\x05\xa1\x43",
                    ble.CH_CAMERA_SSID: b"S5-XXXXXX\x00"}.get(uuid, b"\x01Home\x00")

    return types.SimpleNamespace(BleakClient=Client)


class BleSessionTest(unittest.TestCase):
    def setUp(self):
        self.reg = registry.Registry()
        self.reg.add("S5", client_uuid=PAIRED, ble={"home_ssid": "Home"})
        self.log = []

    def wake(self, cam, **kw):
        async def find(name=None, timeout=15.0):
            return types.SimpleNamespace(name="S5-XXXXXX", address="ADDR")

        async def wifi(ip, timeout):
            return 11.7 if cam.wifi_up else None
        with mock.patch.object(ble, "_bleak", lambda: fake_bleak(cam)), \
                mock.patch.object(ble, "find", find), \
                mock.patch.object(ble, "wait_wifi", wifi), \
                mock.patch.object(asyncio, "sleep", _fast_sleep):
            return asyncio.run(ble.wake(camera_ip="10.0.0.2", log=self.log.append, **kw))

    def test_wake_runs_the_remote_shooting_sequence(self):
        cam = StubCamera()
        res = self.wake(cam, ssid="Home")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["wifi_up_s"] is not None, True)
        seq = [u for u, _ in cam.writes]
        self.assertEqual(seq, ["8d08a420", "cd7a71a0", "ead55e60", "18345be0", "0d6f1880", "0d6f1880", "e182ec40"])
        self.assertEqual([h for u, h in cam.writes if u in ("0d6f1880", "e182ec40")], ["03", "02", "03"])
        self.assertEqual(cam.writes[0][1], "4d454930010010008001" + PAIRED[-12:].lower())
        self.assertEqual(cam.reads[:2], ["e206a5c0", "03e23a31"])   # reads only after the writes
        self.assertTrue(any("notify 18345be1 00" in x for x in self.log), self.log)
        self.assertNotIn("7be5fd56", seq)
        self.assertTrue(self.reg.get(self.reg.active_id())["ble"]["registered_at"])

    def test_old_sequence_does_not_bring_wifi_up(self):
        """Run B: SSID, 03, start 01 -- the stub (like the camera) stays off the network."""
        cam = StubCamera()
        async def go():
            c = ble.LumixBle()
            with mock.patch.object(ble, "_bleak", lambda: fake_bleak(cam)), \
                    mock.patch.object(ble, "find", _find):
                await c.connect()
                await c.login()
                await c.write(ble.CH_WIFI_SSID, ble.ssid_payload("Home"))
                await c.write(ble.CH_WIFI_CMD, b"\x03")
                await c.write(ble.CH_WIFI_START, b"\x01")
        asyncio.run(go())
        self.assertFalse(cam.wifi_up)

    def test_unknown_id_is_a_pairing_problem(self):
        cam = StubCamera(known_id="4D454930-0100-1000-8001-FFFFFFFFFFFF")
        res = self.wake(cam, ssid="Home")
        self.assertFalse(res["ok"])
        self.assertEqual(res["step"], "login")
        self.assertTrue(res["need_pair"])
        self.assertIn("Pairing", res["error"])
        self.assertTrue(res["error"].startswith("login: "), res["error"])
        self.assertEqual([u for u, _ in cam.writes], ["8d08a420", "cd7a71a0"])
        self.assertEqual(cam.reads, [])

    def test_slow_connect_is_retried(self):
        cam = StubCamera(connect_fails=1)
        res = self.wake(cam, ssid="Home")
        self.assertTrue(res["ok"], res)
        self.assertTrue(any("connect try 1 failed: timed out" in x for x in self.log), self.log)

    def test_connect_timeout_names_the_step(self):
        cam = StubCamera(connect_fails=5)
        res = self.wake(cam, ssid="Home")
        self.assertFalse(res["ok"])
        self.assertEqual(res["step"], "connect")
        self.assertTrue(res["error"].startswith("connect: "), res["error"])
        self.assertIn("timed out", res["error"])
        self.assertNotIn("TimeoutError: ", res["error"])

    def test_connect_timeout_is_long_enough_for_a_sleeping_s5(self):
        # the real S5 took 24.2-26.7 s to accept the link from switch-OFF
        self.assertGreaterEqual(ble.CONNECT_TIMEOUT, 40)
        import inspect
        self.assertGreaterEqual(inspect.signature(ble.LumixBle).parameters["connect_tries"].default, 2)

    def test_nonzero_ack_is_reported_not_followed(self):
        cam = StubCamera(ack=b"\x01")
        res = self.wake(cam, ssid="Home")
        self.assertFalse(res["ok"])
        self.assertEqual(res["step"], "wifi")
        self.assertNotIn(("0d6f1880", "01"), cam.writes)    # never switches to the camera's own AP
        self.assertNotIn("e182ec40", [u for u, _ in cam.writes])

    def test_wifi_wait_timeout_names_the_step(self):
        cam = StubCamera(wifi_needs=(9,))          # camera never comes up
        res = self.wake(cam, ssid="Home")
        self.assertFalse(res["ok"])
        self.assertEqual(res["step"], "wifi-wait")
        self.assertIn("10.0.0.2:60606", res["error"])

    def test_describe_never_empty(self):
        self.assertEqual(ble.describe(asyncio.TimeoutError()), "timed out")
        self.assertEqual(ble.describe(RuntimeError("x")), "RuntimeError: x")
        f = ble.error_fields(asyncio.TimeoutError(), "connect")
        self.assertEqual(f, {"error": "connect: timed out", "step": "connect"})


class FixtureReplayTest(unittest.TestCase):
    """The real trace and the code agree on the write sequence of the run that worked."""

    def runs(self):
        out, cur = {}, None
        with open(FIXTURE) as f:
            for line in f:
                m = re.match(r"## run (\w)", line)
                if m:
                    cur = out.setdefault(m.group(1), [])
                    continue
                w = re.search(r"\] write (\w{8}) (\S+)", line)
                if w and cur is not None:
                    cur.append((w.group(1), w.group(2)))
        return out

    def test_fixture_is_scrubbed(self):
        with open(FIXTURE) as f:
            text = f.read()
        for bad in ("4B" + "7775", "192" + ".168.", "/Us" + "ers/"):
            self.assertNotIn(bad, text)

    def test_code_matches_the_working_run(self):
        runs = self.runs()
        good = [(u, h) for u, h in runs["C"] if u not in ("8d08a420", "cd7a71a0", "ead55e60", "18345be0")]
        self.assertEqual(good, [("0d6f1880", "03"), ("0d6f1880", "02"), ("e182ec40", "03")])
        bad = [(u, h) for u, h in runs["B"] if u in ("0d6f1880", "e182ec40")]
        self.assertEqual(bad, [("0d6f1880", "03"), ("e182ec40", "01")])
        cam = StubCamera()
        reg = registry.Registry()
        reg.add("S5", client_uuid=PAIRED)

        async def wifi(ip, timeout):
            return 1.0 if cam.wifi_up else None
        with mock.patch.object(ble, "_bleak", lambda: fake_bleak(cam)), mock.patch.object(ble, "find", _find), \
                mock.patch.object(ble, "wait_wifi", wifi), mock.patch.object(asyncio, "sleep", _fast_sleep):
            res = asyncio.run(ble.wake(ssid="Home", camera_ip="10.0.0.2", log=lambda s: None))
        self.assertTrue(res["ok"], res)
        mine = [(u, h) for u, h in cam.writes if u in ("0d6f1880", "e182ec40")]
        self.assertEqual(mine, good)


class ClientIdTest(unittest.TestCase):
    def test_default_id_does_not_follow_getnode(self):
        with mock.patch.object(registry, "_machine_id", return_value="PLATFORM-UUID-1"):
            a = registry.default_client_uuid()
            with mock.patch("uuid.getnode", return_value=0x112233445566):
                b = registry.default_client_uuid()
            with mock.patch("uuid.getnode", return_value=0x665544332211):
                c = registry.default_client_uuid()
            self.assertEqual(camera._client_uuid(), a)
        self.assertEqual(a, b)
        self.assertEqual(b, c)
        self.assertRegex(a, registry.CLIENT_UUID_RE)

    def test_fallback_without_machine_id(self):
        with mock.patch.object(registry, "_machine_id", return_value=None), \
                mock.patch("uuid.getnode", return_value=0x0A0B0C0D0E0F):
            self.assertEqual(registry.default_client_uuid(), PAIRED)


_real_sleep = asyncio.sleep


async def _fast_sleep(delay, *a, **kw):
    return await _real_sleep(min(delay, 0.02), *a, **kw)


async def _find(name=None, timeout=15.0):
    return types.SimpleNamespace(name="S5-XXXXXX", address="ADDR")


if __name__ == "__main__":
    unittest.main()
