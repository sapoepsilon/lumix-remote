"""BLE session logic against a stub camera (no radio, no bleak).

The stub behaves like the real DC-S5 did on 2026-10-12 with the power switch
OFF and Remote Wakeup ON (tests/fixtures/real-s5-ble-wake-2026-10-12.log):

- an unknown client id is cut off right after the friendly-name write (run A);
- the paired id logs in, the 0d6f1880=03 write is acked on 18345be1;
- Wi-Fi only comes up after the LUMIX Sync Remote-shooting sequence
  SSID, 03, ack, 02, start 03 (run C); SSID, 03, start 01 never got the woken
  camera onto the network (run B).

Behaviour added from the LUMIX Sync 2.0.17 decompile (docs/ble-power-protocol.md;
none of it real-device verified yet), each test cites its source:
- reads after registration: INITIAL_INFORM, camera SSID, AP password (K5/j.java:191-205);
- clock only when the camera asks on 4cf487c0 (I5/g.java:389-391);
- 18345be1 = 00 -> connect type 02, else 01 (I5/g.java:333-366, 471-480);
- e182ec41 = 01 after the start write = "cannot remote wakeup" (I5/g.java:497-509,
  HomeActivity.java:1097-1121);
- from a sleep state, wake with one connection and reconnect once the camera
  advertises "wakeup" (HomeActivity.java:1230-1259).
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

    def __init__(self, known_id=PAIRED, connect_fails=0, ack=b"\x00", wifi_needs=(3, 2),
                 start_ack=None, clock_request=False, notify_ok=False):
        self.known = bytes.fromhex(known_id.replace("-", ""))
        self.connect_fails = connect_fails
        self.ack = ack
        self.wifi_needs = list(wifi_needs)      # 0d6f1880 values a join needs, in order
        self.start_ack = start_ack              # e182ec41 after e182ec40 (None: never sent, as on macOS so far)
        self.clock_request = clock_request      # 4cf487c0 after registration
        self.notify_ok = notify_ok              # start_notify succeeds (a host that sees the CCCDs)
        self.writes = []                        # (short uuid, hex)
        self.reads = []
        self.wifi_up = False
        self.connects = 0
        self.sessions = []                      # writes per connection


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
            cam.connects += 1
            cam.sessions.append([])

        async def disconnect(self):
            self.is_connected = False

        def _drop(self):
            self.is_connected = False
            if self._disc:
                self._disc(self)

        async def start_notify(self, uuid, cb):
            # macOS finds no CCCDs on the S5, but the camera notifies anyway
            self._cbs[uuid] = cb
            if not cam.notify_ok:
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
            cam.sessions[-1].append((uuid[:8], data.hex()))
            if uuid == ble.CH_DLNA_UUID:
                self._id_ok = data == cam.known
            elif uuid == ble.CH_FRIENDLY_NAME and not self._id_ok:
                self._drop()                    # run A: HCI 0x13 right after the name write
                raise RuntimeError("disconnected")
            elif uuid == ble.CH_FRIENDLY_NAME and cam.clock_request:
                asyncio.get_running_loop().call_later(0.01, self._notify, ble.CH_CLOCK_NOTIFY, b"\x01")
            elif uuid == ble.CH_WIFI_CMD:
                if data == bytes([ble.WIFI_STA_SSID_SET]):
                    asyncio.get_running_loop().call_later(0.01, self._notify, ble.CH_WIFI_CMD_ACK, cam.ack)
            elif uuid == ble.CH_WIFI_START:
                cmds = [int(h, 16) for u, h in cam.writes if u == ble.CH_WIFI_CMD[:8]]
                cam.wifi_up = (cmds[-len(cam.wifi_needs):] == cam.wifi_needs and data == b"\x03"
                               and cam.start_ack != b"\x01")
                if cam.start_ack is not None:
                    asyncio.get_running_loop().call_later(0.01, self._notify, ble.CH_WIFI_START_ACK, cam.start_ack)

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

    def wake(self, cam, adv_states=(0,), **kw):
        """adv_states: state bytes the camera advertises on successive scans
        (the last one repeats); empty = not seen by the pre-wake scan.
        Default: awake (0 normal)."""
        states = list(adv_states)
        self.scans = 0

        async def scan(timeout=8.0, name=None, stop_on_first=False, mfr_addr=None, stop_on_state=False):
            self.scans += 1
            if not states:
                return []
            st = states.pop(0) if len(states) > 1 else states[0]
            return [{"name": "S5-XXXXXX", "address": "ADDR%d" % self.scans, "rssi": -60,
                     "mfr": "%02x0a0b0c0d0e" % st, "mfr_addr": "0a0b0c0d0e", "state_byte": st,
                     "state": ble.ADV_STATES.get(st)}]

        async def wifi(ip, timeout):
            return 11.7 if cam.wifi_up else None
        with mock.patch.object(ble, "_bleak", lambda: fake_bleak(cam)), \
                mock.patch.object(ble, "find", _find), \
                mock.patch.object(ble, "scan", scan), \
                mock.patch.object(ble, "wait_wifi", wifi), \
                mock.patch.object(ble, "START_ACK_WAIT", 0.05), \
                mock.patch.object(asyncio, "sleep", _fast_sleep):
            return asyncio.run(ble.wake(camera_ip="10.0.0.2", log=self.log.append, **kw))

    def test_wake_runs_the_remote_shooting_sequence(self):
        cam = StubCamera()
        res = self.wake(cam, ssid="Home")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["wifi_up_s"] is not None, True)
        seq = [u for u, _ in cam.writes]
        # no unrequested clock write (ead55e60): I5/g.java:389-391
        self.assertEqual(seq, ["8d08a420", "cd7a71a0", "18345be0", "0d6f1880", "0d6f1880", "e182ec40"])
        self.assertEqual([h for u, h in cam.writes if u in ("0d6f1880", "e182ec40")], ["03", "02", "03"])
        self.assertEqual(cam.writes[0][1], "4d454930010010008001" + PAIRED[-12:].lower())
        # reads only after the writes, in K5/j.java:191-205 order
        self.assertEqual(cam.reads[:3], ["03e23a31", "e206a5c0", "c97cf1a5"])
        self.assertTrue(any("notify 18345be1 00" in x for x in self.log), self.log)
        self.assertTrue(all(x.startswith("ble.trace ") for x in self.log), self.log)
        self.assertEqual(res["mode"], "single-session")     # awake (adv state 0): one connection
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

    def test_refusal_on_e182ec41_is_reported(self):
        # e182ec41 = 01 -> ON_BT_CANNOT_REMOTE_WAKEUP (I5/g.java:497-509, HomeActivity.java:1097-1121)
        cam = StubCamera(start_ack=b"\x01", notify_ok=True)
        res = self.wake(cam, ssid="Home")
        self.assertFalse(res["ok"])
        self.assertEqual(res["step"], "wifi")
        self.assertTrue(res["refused"])
        self.assertEqual(res["error"], "wifi: camera refused remote wake-up (camera state)")
        self.assertIn("isn't busy", res["hint"])
        self.assertIn([ble.CH_WIFI_START_ACK, "01"], [[u, h] for _, u, h in res["notifies"]])

    def test_start_ack_ok_is_recorded(self):
        cam = StubCamera(start_ack=b"\x00", notify_ok=True)
        res = self.wake(cam, ssid="Home")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["wifi_start_result"], "00")

    def test_start_ack_not_awaited_without_subscriptions(self):
        # Real S5 on macOS: all 10 start_notify calls fail, so e182ec41 can't be
        # seen; don't spend START_ACK_WAIT on it and say so.
        cam = StubCamera(start_ack=b"\x01")    # would refuse, but this host can't observe it
        res = self.wake(cam, ssid="Home")
        self.assertEqual(res["notify_on"], 0)
        self.assertIsNone(res["wifi_start_result"])
        self.assertNotIn("refused", res)
        self.assertTrue(any("refusal can't be observed" in x for x in self.log), self.log)
        self.assertFalse(any("wifi start result: none in" in x for x in self.log), self.log)

    def test_snapshot_waits_for_a_state_byte(self):
        # An advertisement without manufacturer data must not end the pre-scan.
        calls = []

        async def scan(timeout=8.0, name=None, **kw):
            calls.append((timeout, kw))
            return [{"name": "S5-XXXXXX", "address": "A", "state_byte": None, "mfr_addr": None, "_dev": 1},
                    {"name": "S5-XXXXXX", "address": "B", "state_byte": 6, "state": "sleep_pow_off_fast",
                     "mfr_addr": "0a0b", "_dev": 2}]
        with mock.patch.object(ble, "scan", scan):
            adv = asyncio.run(ble.adv_snapshot("S5-XXXXXX", ble.ADV_SLEEP_GAP_S, log=self.log.append))
        self.assertEqual(calls, [(ble.ADV_SLEEP_GAP_S, {"stop_on_state": True})])
        self.assertEqual(adv["state_byte"], 6)
        self.assertNotIn("_dev", adv)

    def test_unknown_state_still_uses_two_sessions(self):
        # Pre-scan saw nothing for ADV_SLEEP_GAP_S: two-session anyway, loudly.
        cam = StubCamera()
        res = self.wake(cam, adv_states=(), ssid="Home", wake_wait=0.1)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["mode"], "two-session")
        self.assertIsNone(res["adv_state_before"])
        self.assertEqual(cam.connects, 2)
        self.assertEqual([u for u, _ in cam.sessions[0]], ["8d08a420", "cd7a71a0"])     # login only
        self.assertTrue(any("WARNING: no advertised state" in x for x in self.log), self.log)

    def test_clock_written_only_when_the_camera_asks(self):
        # 4cf487c0 -> write clock (type 9), the only clock write in the app (I5/g.java:389-391)
        cam = StubCamera(clock_request=True)
        res = self.wake(cam, ssid="Home")
        self.assertTrue(res["ok"], res)
        self.assertEqual([u for u, _ in cam.writes].count("ead55e60"), 1)
        self.assertTrue(any("notify 4cf487c0" in x for x in self.log), self.log)

    def test_two_session_wake_from_sleep(self):
        # sleep_pow_off_fast -> stage-1 login, wait for "wakeup", reconnect (HomeActivity.java:1230-1259)
        cam = StubCamera()
        res = self.wake(cam, adv_states=(6, 6, 2), ssid="Home")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["mode"], "two-session")
        self.assertEqual(res["adv_state_before"], 6)
        self.assertEqual(res["adv_state_after_wake"], 2)
        self.assertEqual(cam.connects, 2)
        first, second = cam.sessions
        self.assertEqual([u for u, _ in first], ["8d08a420", "cd7a71a0"])     # login only
        self.assertEqual([h for u, h in second if u in ("0d6f1880", "e182ec40")], ["03", "02", "03"])
        self.assertIn("note", res)                           # _fast state: LUMIX Sync would use SoftAP
        self.assertTrue(any("adv state 2 wakeup" in x for x in self.log), self.log)

    def test_single_session_flag_keeps_one_connection(self):
        cam = StubCamera()
        res = self.wake(cam, adv_states=(6,), ssid="Home", single_session=True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(cam.connects, 1)
        self.assertEqual(res["mode"], "single-session")

    def test_awake_camera_needs_one_session(self):
        cam = StubCamera()
        res = self.wake(cam, adv_states=(0,), ssid="Home")
        self.assertTrue(res["ok"], res)
        self.assertEqual(cam.connects, 1)

    def test_legacy_baseline_keeps_the_old_bytes(self):
        cam = StubCamera()
        res = self.wake(cam, adv_states=(6,), ssid="Home", legacy=True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["mode"], "legacy")
        self.assertEqual([u for u, _ in cam.writes],
                         ["8d08a420", "cd7a71a0", "ead55e60", "18345be0", "0d6f1880", "0d6f1880", "e182ec40"])
        self.assertEqual(cam.reads[:2], ["e206a5c0", "03e23a31"])

    def test_nonzero_ack_with_softap_writes_01(self):
        # 18345be1 != 00 -> connect type 01 (I5/g.java:333-366, 471-480); only behind --softap
        cam = StubCamera(ack=b"\x01")
        async def go():
            c = ble.LumixBle()
            with mock.patch.object(ble, "_bleak", lambda: fake_bleak(cam)), \
                    mock.patch.object(ble, "find", _find), mock.patch.object(ble, "START_ACK_WAIT", 0.05):
                await c.connect()
                await c.login()
                await c.wifi_join("Home", softap=True)
        asyncio.run(go())
        self.assertEqual([h for u, h in cam.writes if u in ("0d6f1880", "e182ec40")], ["03", "01", "03"])

    def test_softap_purpose_5_sequence(self):
        # purpose 5: connect type 01, then e182ec40 = 05 (I5/g.java:936-967)
        cam = StubCamera()
        async def go():
            c = ble.LumixBle()
            with mock.patch.object(ble, "_bleak", lambda: fake_bleak(cam)), \
                    mock.patch.object(ble, "find", _find), mock.patch.object(ble, "START_ACK_WAIT", 0.05):
                await c.connect()
                await c.login()
                await c.wifi_join(purpose=ble.PURPOSE_SOFTAP, softap=True)
                return c
        c = asyncio.run(go())
        self.assertEqual([(u, h) for u, h in cam.writes if u in ("0d6f1880", "e182ec40")],
                         [("0d6f1880", "01"), ("e182ec40", "05")])
        self.assertEqual(c._ap[0], "S5-XXXXXX")       # camera AP SSID kept for the join

    def test_notify_retried_after_registration(self):
        cam = StubCamera()
        log = []
        async def go():
            c = ble.LumixBle(on_event=log.append)
            with mock.patch.object(ble, "_bleak", lambda: fake_bleak(cam)), mock.patch.object(ble, "find", _find):
                await c.connect()
                await c.login()
        asyncio.run(go())
        self.assertTrue(any("notify subscriptions after registration: 0/10 on" in x for x in log), log)
        self.assertTrue(any("notify e182ec41 still refused" in x for x in log), log)

    def test_adv_match_uses_name_or_mfr_address(self):
        m = bytes.fromhex("060a0b0c0d0e")
        self.assertTrue(ble.adv_matches("S5-XXXXXX", m, "S5-XXXXXX", None))
        self.assertTrue(ble.adv_matches("", m, "S5-XXXXXX", "0a0b0c0d0e"))    # rotated, nameless
        self.assertFalse(ble.adv_matches("S5-YYYYYY", bytes.fromhex("06ffffffffff"), "S5-XXXXXX", "0a0b0c0d0e"))
        self.assertTrue(ble.adv_matches("S5-YYYYYY", m, None, None))

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
        async def scan(*a, **kw):
            return [{"name": "S5-XXXXXX", "address": "ADDR", "rssi": -60, "mfr": "000a0b0c0d0e",
                     "mfr_addr": "0a0b0c0d0e", "state_byte": 0, "state": ble.ADV_STATES.get(0)}]
        with mock.patch.object(ble, "_bleak", lambda: fake_bleak(cam)), mock.patch.object(ble, "find", _find), \
                mock.patch.object(ble, "scan", scan), mock.patch.object(ble, "START_ACK_WAIT", 0.05), \
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


async def _find(name=None, timeout=15.0, **kw):
    return types.SimpleNamespace(name="S5-XXXXXX", address="ADDR")


if __name__ == "__main__":
    unittest.main()
