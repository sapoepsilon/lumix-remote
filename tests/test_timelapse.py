"""Timelapse job model, validation, persistence, CLI helpers and the quiesce
plan (no camera, no network). End-to-end runs: test_timelapse_emu.py."""
import isolate  # noqa: F401  (temp config dir; must come first)

import asyncio
import datetime as dt
import os
import tempfile
import time
import unittest
from types import SimpleNamespace

from lumix.files import ImportJob
from lumix.timelapse import TimelapseEngine, TimelapseError, TimelapseJob, parse_start_at
from lumixcli.main import parse_duration
from lumixcli.resolve import ResolveError
from server import quiesce


class FakeBus:
    def __init__(self):
        self.msgs = []

    def publish(self, kind, data):
        self.msgs.append((kind, data))


def fake_cam(native=True, **state):
    st = {"cammode": "rec"}
    st.update(state)
    return SimpleNamespace(
        status="ready", state=st, bus=FakeBus(), interval_owner=None, connected_at=time.time(),
        powered_off=False, profile=SimpleNamespace(capabilities={"interval_native": native,
                                                                 "interval_max_count": 9999}),
        log_event=lambda *a, **k: None, _lv_running=True,
        liveview=SimpleNamespace(age=0.1, frame=None),
        listener=SimpleNamespace(sid="uuid:x"))


class JobMathTest(unittest.TestCase):
    def test_count_duration_target(self):
        j = TimelapseJob(interval_s=10, count=None, duration_s=60)
        self.assertEqual((j.slot_limit, j.target), (7, 7))        # 0, 10, ... 60 s
        j = TimelapseJob(interval_s=10, count=5, duration_s=60)
        self.assertEqual(j.target, 5)
        j = TimelapseJob(interval_s=10, count=360)
        self.assertIsNone(j.slot_limit)
        self.assertEqual(j.target, 360)

    def test_finished_and_remaining(self):
        j = TimelapseJob(interval_s=5, count=3, state="running")
        j.shots, j.slot = 2, 4             # two skipped slots extend the run
        self.assertFalse(j.finished_shooting())
        self.assertEqual(j.remaining(), 1)
        j.shots = 3
        self.assertTrue(j.finished_shooting())
        j = TimelapseJob(interval_s=5, duration_s=20, state="running")
        j.slot = 5
        self.assertTrue(j.finished_shooting())

    def test_grid_and_eta(self):
        j = TimelapseJob(interval_s=10, count=4, state="running")
        j.reanchor(1000.0)
        self.assertEqual([j.slot_time(k) for k in range(3)], [1000.0, 1010.0, 1020.0])
        j.slot, j.shots = 2, 2
        j.reanchor(2000.0)                 # resume after a pause: grid restarts at "now"
        self.assertEqual(j.slot_time(), 2000.0)
        self.assertEqual(j.slot_time(3), 2010.0)
        j.next_shot_at = 2000.0
        self.assertEqual(j.eta_s(now=1995.0), 5.0 + 10.0)    # next in 5 s, then one more
        self.assertEqual(j.progress(), 0.5)

    def test_round_trip(self):
        j = TimelapseJob(name="x", interval_s=3, count=9, ae_lock=True, fixed={"focal": "1024/256"})
        j.shots, j.frames = 4, ["/a.jpg"]
        k = TimelapseJob.from_dict(j.save_dict())
        self.assertEqual((k.name, k.shots, k.frames, k.fixed), ("x", 4, ["/a.jpg"], {"focal": "1024/256"}))
        d = j.to_dict()
        self.assertEqual((d["target"], d["frames_total"]), (9, 1))


class StartAtTest(unittest.TestCase):
    def test_forms(self):
        now = dt.datetime(2026, 10, 12, 18, 0).timestamp()
        self.assertIsNone(parse_start_at(None))
        self.assertEqual(parse_start_at(1234.5), 1234.5)
        self.assertEqual(parse_start_at("+90", now), now + 90)
        self.assertEqual(parse_start_at("18:30", now), now + 1800)
        self.assertEqual(parse_start_at("06:00", now), now + 12 * 3600)       # tomorrow
        self.assertEqual(parse_start_at("2026-10-12T19:00", now), now + 3600)
        with self.assertRaises(TimelapseError):
            parse_start_at("soon")

    def test_cli_durations(self):
        self.assertEqual([parse_duration(x) for x in ("90", "90s", "30m", "1h30m", "2:30", "1:00:00")],
                         [90, 90, 1800, 5400, 150, 3600])
        with self.assertRaises(ResolveError):
            parse_duration("ten")


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def eng(self, native=True):
        return TimelapseEngine(fake_cam(native), SimpleNamespace(active=False), self.tmp.name)

    def test_rules(self):
        e = self.eng()
        with self.assertRaisesRegex(TimelapseError, "count"):
            e.new_job({"interval": 5})
        with self.assertRaisesRegex(TimelapseError, "interval"):
            e.new_job({"interval": 0.5, "count": 3})
        with self.assertRaisesRegex(TimelapseError, "interval"):
            e.new_job({"interval": "x", "count": 3})
        j = e.new_job({"interval": 5, "count": 3, "video": True, "name": "my sunset!"})
        self.assertTrue(j.auto_import)                 # MP4 needs the frames
        self.assertEqual(j.import_mode, "end")         # 5 s: no room for a play-mode trip
        self.assertEqual(j.name, "my-sunset")
        self.assertTrue(j.folder.endswith(os.path.join("timelapse-my-sunset")))
        self.assertEqual(e.new_job({"interval": 60, "count": 3, "import": True}).import_mode, "each")
        n = e.new_job({"interval": 10, "duration": 60, "native": True, "import": True})
        self.assertEqual((n.engine, n.count, n.duration_s, n.import_mode), ("native", 7, None, "end"))
        with self.assertRaisesRegex(TimelapseError, "whole seconds"):
            e.new_job({"interval": 2.5, "count": 3, "native": True})
        with self.assertRaisesRegex(TimelapseError, "interval_native"):
            self.eng(native=False).new_job({"interval": 5, "count": 3, "native": True})

    def test_persistence_and_restart_pickup(self):
        e = self.eng()
        e.job = TimelapseJob(name="p", interval_s=5, count=3, state="paused", paused_by="server-stop",
                             started_at=time.time() - 90,
                             paused_at=time.time() - 60)
        e.save()
        e2 = self.eng()
        self.assertEqual((e2.job.name, e2.job.state, e2.job.paused_by), ("p", "paused", "server-stop"))

        async def go():
            # "reconnect" resumes only for a connection made after the pause
            e2.cam.connected_at = time.time() - 120
            self.assertFalse(await e2.auto_resume("reconnect"))
            self.assertFalse(await e2.auto_resume("files"))
            e2.cam.connected_at = time.time()
            e2._spawn = lambda coro=None: coro.close() if coro else None     # no runner here
            self.assertTrue(await e2.auto_resume("reconnect"))
            self.assertEqual(e2.job.state, "running")
            self.assertIsNone(e2.job.anchor_t)       # re-anchored on the next slot
        asyncio.new_event_loop().run_until_complete(go())


class ImportCheckpointTest(unittest.TestCase):
    def test_running_job_loads_as_paused(self):
        j = ImportJob(["1", "2"], "/dest", False, folder="/dest/x", label="t")
        j.state, j.saved, j.files_done = "running", ["/dest/x/P1.JPG"], 1
        k = ImportJob.from_checkpoint(j.checkpoint())
        self.assertEqual((k.id, k.state, k.paused_by, k.saved, k.folder, k.files_done),
                         (j.id, "paused", "server-stop", ["/dest/x/P1.JPG"], "/dest/x", 1))


class PlanTest(unittest.TestCase):
    def test_plan_lists_what_runs(self):
        cam = fake_cam(rec="on")
        imp = ImportJob(["1", "2", "3"], "/d", False)
        imp.state, imp.files_total, imp.files_done = "running", 3, 1
        files = SimpleNamespace(running_jobs=lambda: [imp], active=False)
        tl = SimpleNamespace(job=TimelapseJob(name="s", interval_s=5, count=10, state="running"))
        tl.job.shots = 4
        sess = SimpleNamespace(cam=cam, files=files, timelapse=tl, resume_on_reconnect=lambda: True)
        p = quiesce.plan(sess, "poweroff")
        self.assertEqual([i["kind"] for i in p["items"]],
                         ["import", "recording", "timelapse", "liveview", "events"])
        self.assertEqual(p["items"][0]["label"], "Import: 1 of 3 files")
        self.assertEqual(p["items"][2]["label"], "Timelapse s: 4 of 10 shots")
        # files -> shooting only checkpoints imports; shooting -> files keeps imports running
        self.assertEqual([i["kind"] for i in quiesce.plan(sess, "shooting")["items"]], ["import"])
        self.assertEqual([i["kind"] for i in quiesce.plan(sess, "files")["items"]],
                         ["recording", "timelapse"])
        with self.assertRaises(ValueError):
            quiesce.plan(sess, "nap")


if __name__ == "__main__":
    unittest.main()
