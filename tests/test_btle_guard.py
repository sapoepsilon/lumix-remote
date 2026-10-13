"""BTLEServer CPU guard and BLE scan/connect leak checks (no radio, `ps` mocked).

macOS's /usr/sbin/BTLEServer has stuck at ~100% CPU for hours after BLE wake
attempts and freezes Bluetooth mice, so lumix.ble refuses to scan or connect
while it is hot, and never leaves a scan running or a link half-open.
"""
import isolate  # noqa: F401  (temp config dir + env; must come first)

import asyncio
import os
import subprocess
import types
import unittest
from unittest import mock

from lumix import ble
from server import ble_api
from test_ble_wake import stub_ble
from test_timelapse_emu import EmuTestCase

MSG = "macOS Bluetooth daemon (BTLEServer) is stuck at 100% CPU; run: sudo killall -9 BTLEServer"


def ps_out(*rows):
    return "  %CPU COMMAND\n" + "".join("%5.1f %s\n" % r for r in rows)


def fake_ps(samples):
    """subprocess.run stand-in: each call returns the next ps listing (the last repeats)."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        text = samples[min(len(calls) - 1, len(samples) - 1)]
        return subprocess.CompletedProcess(cmd, 0, stdout=text, stderr="")
    run.calls = calls
    return run


HOT = ps_out((0.0, "/usr/sbin/BTLEServerAgent"), (99.7, "/usr/sbin/BTLEServer"), (3.0, "/bin/zsh"))
COLD = ps_out((0.0, "/usr/sbin/BTLEServerAgent"), (1.2, "/usr/sbin/BTLEServer"))
NONE = ps_out((5.0, "/bin/zsh"), (99.0, "/usr/sbin/BTLEServerAgent"))


class GuardCase(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("LUMIX_BLE_SKIP_BTLE_CHECK", None)

    def ps(self, *samples):
        run = fake_ps(list(samples))
        p = mock.patch("subprocess.run", run)
        p.start()
        self.addCleanup(p.stop)
        return run


class BtleCpuTest(GuardCase):
    def test_reads_ps_pcpu_comm_and_ignores_the_agent(self):
        run = self.ps(HOT)
        self.assertEqual(ble.btle_cpu(), 99.7)
        self.assertEqual(run.calls[0], ["ps", "-Ao", "pcpu,comm"])
        self.ps(NONE)
        self.assertIsNone(ble.btle_cpu())           # only BTLEServerAgent is hot: not the daemon

    def test_ps_failure_is_not_stuck(self):
        with mock.patch("subprocess.run", side_effect=OSError("no ps")):
            self.assertIsNone(ble.btle_cpu())


class CheckTest(GuardCase):
    def check(self, gap=0.0):
        return asyncio.run(ble.check_btle_server(gap))

    def test_hot_in_both_samples_refuses_with_the_exact_message(self):
        run = self.ps(HOT, HOT)
        with self.assertRaises(ble.BtleServerStuck) as cm:
            self.check()
        self.assertEqual(str(cm.exception), MSG)
        self.assertEqual(len(run.calls), 2)
        f = ble.error_fields(cm.exception)
        self.assertEqual((f["error"], f["step"], f["btle_stuck"]), (MSG, "bluetooth", True))

    def test_samples_are_a_second_apart(self):
        self.ps(HOT, HOT)
        sleeps = []

        async def sleep(t):
            sleeps.append(t)
        with mock.patch.object(asyncio, "sleep", sleep), self.assertRaises(ble.BtleServerStuck):
            asyncio.run(ble.check_btle_server())
        self.assertEqual(sleeps, [1.0])

    def test_a_spike_that_clears_is_allowed(self):
        run = self.ps(HOT, COLD)
        self.check()
        self.assertEqual(len(run.calls), 2)

    def test_cold_or_missing_needs_one_sample(self):
        for out in (COLD, NONE):
            run = self.ps(out)
            self.check()
            self.assertEqual(len(run.calls), 1)

    def test_exactly_80_is_not_stuck(self):
        self.ps(ps_out((80.0, "/usr/sbin/BTLEServer")))
        self.check()

    def test_env_opt_out(self):
        run = self.ps(HOT, HOT)
        os.environ["LUMIX_BLE_SKIP_BTLE_CHECK"] = "1"
        self.check()
        self.assertEqual(run.calls, [])


class FakeScanner:
    made = []

    def __init__(self, cb=None, hang_start=False):
        self.starts = self.stops = 0
        self.hang_start = hang_start
        FakeScanner.made.append(self)

    async def start(self):
        self.starts += 1
        if self.hang_start:
            await asyncio.sleep(3600)

    async def stop(self):
        self.stops += 1


class FakeClient:
    made = []
    connect_exc = asyncio.TimeoutError

    def __init__(self, dev, disconnected_callback=None, timeout=None):
        self.connects = self.disconnects = 0
        self.is_connected = False
        FakeClient.made.append(self)

    async def connect(self):
        self.connects += 1
        if FakeClient.connect_exc is None:
            await asyncio.sleep(3600)       # a connect that never returns
        raise FakeClient.connect_exc()

    async def disconnect(self):
        self.disconnects += 1


def fake_bleak(**kw):
    return types.SimpleNamespace(BleakScanner=lambda cb: FakeScanner(cb, **kw), BleakClient=FakeClient)


class LeakCase(GuardCase):
    def setUp(self):
        super().setUp()
        FakeScanner.made, FakeClient.made = [], []
        FakeClient.connect_exc = asyncio.TimeoutError
        os.environ["LUMIX_BLE_SKIP_BTLE_CHECK"] = "1"      # these tests are about leaks, not the guard
        self.addCleanup(lambda: (ble._LIVE_SCANNERS.clear(), ble._LIVE_CLIENTS.clear()))


class ScanLeakTest(LeakCase):
    def test_scan_refuses_before_touching_bleak_when_stuck(self):
        os.environ.pop("LUMIX_BLE_SKIP_BTLE_CHECK")
        self.ps(HOT, HOT)

        def boom():
            raise AssertionError("bleak must not be loaded while BTLEServer is stuck")
        with mock.patch.object(ble, "_bleak", boom), mock.patch.object(ble, "BTLE_SAMPLE_GAP", 0.0), \
                self.assertRaises(ble.BtleServerStuck):
            asyncio.run(ble.scan(0.1))
        self.assertEqual(FakeScanner.made, [])

    def test_scan_stops_the_scanner(self):
        with mock.patch.object(ble, "_bleak", lambda: fake_bleak()):
            asyncio.run(ble.scan(0.3))
        (sc,) = FakeScanner.made
        self.assertEqual((sc.starts, sc.stops), (1, 1))
        self.assertFalse(ble._LIVE_SCANNERS)

    def test_scan_stops_the_scanner_when_cancelled(self):
        async def go():
            t = asyncio.ensure_future(ble.scan(60))
            await asyncio.sleep(0.4)
            t.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await t
        with mock.patch.object(ble, "_bleak", lambda: fake_bleak()):
            asyncio.run(go())
        (sc,) = FakeScanner.made
        self.assertEqual(sc.stops, 1)
        self.assertFalse(ble._LIVE_SCANNERS)

    def test_scan_stops_a_scanner_whose_start_hangs(self):
        with mock.patch.object(ble, "_bleak", lambda: fake_bleak(hang_start=True)), \
                mock.patch.object(ble, "SCAN_START_TIMEOUT", 0.1):
            with self.assertRaises(ble.BleStepError) as cm:
                asyncio.run(ble.scan(5))
        self.assertEqual(cm.exception.step, "scan")
        self.assertEqual(FakeScanner.made[0].stops, 1)

    def test_wait_state_does_not_swallow_a_stuck_daemon(self):
        async def scan(*a, **k):
            raise ble.BtleServerStuck()
        with mock.patch.object(ble, "scan", scan), self.assertRaises(ble.BtleServerStuck):
            asyncio.run(ble.wait_state(None, None, timeout=5))


class ConnectLeakTest(LeakCase):
    def setUp(self):
        super().setUp()

        async def find(*a, **k):
            return types.SimpleNamespace(name="S5-XXXXXX", address="ADDR")
        for p in (mock.patch.object(ble, "_bleak", lambda: fake_bleak()), mock.patch.object(ble, "find", find)):
            p.start()
            self.addCleanup(p.stop)

    def with_cam(self, fn, **kw):
        """Run ``fn(LumixBle)`` on a fresh loop (asyncio.Event binds to it on 3.9)."""
        async def go():
            return await fn(ble.LumixBle("S5-XXXXXX", **kw))
        return asyncio.run(go())

    def test_two_connect_timeouts_disconnect_every_client(self):
        with self.assertRaises(ble.BleStepError) as cm:
            self.with_cam(lambda c: c.connect(), connect_tries=2)
        self.assertEqual(cm.exception.step, "connect")
        self.assertEqual([(x.connects, x.disconnects) for x in FakeClient.made], [(1, 1), (1, 1)])
        self.assertFalse(ble._LIVE_CLIENTS)

    def test_cancelled_connect_disconnects(self):
        FakeClient.connect_exc = None          # connect() never returns

        async def go(c):
            t = asyncio.ensure_future(c.connect())
            await asyncio.sleep(0.2)
            t.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await t
        self.with_cam(go, connect_tries=2)
        self.assertEqual([(x.connects, x.disconnects) for x in FakeClient.made], [(1, 1)])
        self.assertFalse(ble._LIVE_CLIENTS)

    def test_failed_disconnect_is_still_forgotten(self):
        async def bad(self):
            raise RuntimeError("boom")
        with mock.patch.object(FakeClient, "disconnect", bad), self.assertRaises(ble.BleStepError):
            self.with_cam(lambda c: c.connect(), connect_tries=1)
        self.assertFalse(ble._LIVE_CLIENTS)

    def test_close_disconnects_a_link_that_is_not_yet_connected(self):
        async def go(c):
            c.client = FakeClient(None)        # is_connected False, e.g. still being set up
            await c.close()
            return c.client
        self.assertEqual(self.with_cam(go).disconnects, 1)

    def test_connect_refuses_when_stuck(self):
        os.environ.pop("LUMIX_BLE_SKIP_BTLE_CHECK")
        self.ps(HOT, HOT)
        with mock.patch.object(ble, "BTLE_SAMPLE_GAP", 0.0), self.assertRaises(ble.BtleServerStuck):
            self.with_cam(lambda c: c.connect())
        self.assertEqual(FakeClient.made, [])

    def test_stuck_after_a_failed_try_stops_the_second_try(self):
        os.environ.pop("LUMIX_BLE_SKIP_BTLE_CHECK")
        # connect() entry: cold; after the first timeout: hot, hot -> refuse instead of retrying
        self.ps(COLD, HOT, HOT)
        with mock.patch.object(ble, "BTLE_SAMPLE_GAP", 0.0), self.assertRaises(ble.BtleServerStuck):
            self.with_cam(lambda c: c.connect(), connect_tries=2)
        self.assertEqual([(x.connects, x.disconnects) for x in FakeClient.made], [(1, 1)])


class WakeLeakTest(LeakCase):
    def run_wake(self):
        async def scan(*a, **k):
            return []

        async def find(*a, **k):
            return types.SimpleNamespace(name="S5-XXXXXX", address="ADDR")
        with mock.patch.object(ble, "_bleak", lambda: fake_bleak()), mock.patch.object(ble, "scan", scan), \
                mock.patch.object(ble, "find", find):
            return asyncio.run(ble.wake(camera_ip="10.0.0.2", log=lambda s: None))

    def test_wake_that_times_out_twice_leaves_nothing_running(self):
        res = self.run_wake()
        self.assertFalse(res["ok"])
        self.assertEqual(res["step"], "connect")
        self.assertTrue(FakeClient.made)
        self.assertTrue(all(x.disconnects >= x.connects for x in FakeClient.made))
        self.assertFalse(ble._LIVE_CLIENTS)
        self.assertFalse(ble._LIVE_SCANNERS)

    def test_release_all_stops_strays(self):
        sc, cl = FakeScanner(), FakeClient(None)
        ble._LIVE_SCANNERS.add(sc)
        ble._LIVE_CLIENTS.add(cl)
        asyncio.run(ble.release_all())
        self.assertEqual((sc.stops, cl.disconnects), (1, 1))
        self.assertFalse(ble._LIVE_SCANNERS or ble._LIVE_CLIENTS)

    def test_wake_refused_when_stuck_never_scans(self):
        os.environ.pop("LUMIX_BLE_SKIP_BTLE_CHECK")
        self.ps(HOT, HOT)
        with mock.patch.object(ble, "BTLE_SAMPLE_GAP", 0.0), \
                mock.patch.object(ble, "_bleak", lambda: self.fail("bleak loaded")):
            res = asyncio.run(ble.wake(camera_ip="10.0.0.2", log=lambda s: None))
        self.assertEqual((res["ok"], res["error"], res["step"], res["btle_stuck"]), (False, MSG, "bluetooth", True))


class ServerGuardTest(EmuTestCase):
    """The server refuses before spawning lumix-ble, and a refused wake costs no wake-cap slot."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("LUMIX_BLE_SKIP_BTLE_CHECK", None)

    def test_wake_power_cycle_and_probe_refuse_when_stuck(self):
        r = self.rig
        stub = stub_ble(os.path.join(self.tmp.name, "lumix-ble"), '{"ok": true}')
        spawned = os.path.join(self.tmp.name, "spawned")
        with open(stub, "a") as f:
            f.write("open(%r, 'w').write('x')\n" % spawned)

        async def go():
            with mock.patch.object(ble_api, "BLE_BIN", stub), mock.patch("subprocess.run", fake_ps([HOT])), \
                    mock.patch.object(ble, "BTLE_SAMPLE_GAP", 0.0):
                for path in ("wake", "power-cycle"):
                    st, d = await r.call("POST", "/api/cameras/emu/ble/" + path, {})
                    self.assertEqual(st, 502, d)
                    self.assertEqual((d["error"], d["step"], d["btle_stuck"]), (MSG, "bluetooth", True))
                st, d = await r.call("GET", "/api/cameras/emu/ble/status?probe=1")
                self.assertEqual(d["probe"]["error"], MSG)
            self.assertFalse(os.path.exists(spawned))              # lumix-ble never ran
            st, s = await r.call("GET", "/api/cameras/emu/ble/status")
            self.assertEqual(s["wakes"]["recent"], 0)              # no battery-cap slot used
        self.run_(go(), 30)


class UiTest(unittest.TestCase):
    def test_page_shows_the_daemon_error(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "server", "static", "app.js")) as f:
            js = f.read()
        self.assertIn("function btleStuck", js)
        self.assertIn("btle_stuck", js)
        self.assertIn("Bluetooth is stuck on this computer", js)


if __name__ == "__main__":
    unittest.main()
