#!/usr/bin/env python3
"""Run the chaos harness (tests/chaos_harness.py) against the camera emulator.

    .venv/bin/python tools/chaos.py --seeds 1-50 --duration 20 --tabs 3 --restarts 1

Each seed gets its own emulator, server (ephemeral port) and temp config dir;
nothing talks to a real camera. Exit status 1 if any seed broke an invariant.
"""
import argparse
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))
sys.path.insert(0, os.path.join(HERE, ".."))
os.environ.setdefault("LUMIX_TEST_HOME", tempfile.mkdtemp(prefix="lumix-chaos-home-"))
os.environ["LUMIX_REMOTE_HOME"] = os.environ["LUMIX_TEST_HOME"]

import chaos_harness  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", default="1-10", help="e.g. 1-50 or 3,7,12")
    ap.add_argument("--duration", type=float, default=20.0, help="seconds of traffic per seed")
    ap.add_argument("--tabs", type=int, default=3, help="concurrent browser tabs")
    ap.add_argument("--restarts", type=int, default=1, help="server restarts per seed")
    ap.add_argument("--timing", choices=["fast", "real"], default="fast",
                    help="fast = camera and server timing x0.05; real = the real cap and settle windows")
    ap.add_argument("--only", help="comma-separated action names (default: all)")
    ap.add_argument("-v", action="store_true", help="print the trace of passing seeds too")
    a = ap.parse_args()
    actions = None
    if a.only:
        actions = {n: chaos_harness.ACTIONS[n] for n in a.only.split(",")}
    failed = []
    for seed in chaos_harness.parse_seeds(a.seeds):
        t = time.monotonic()
        r = chaos_harness.run_seed(seed, a.duration, a.tabs, a.restarts, actions, timing=a.timing)
        print(r.report() + "  (%.0f s, %s)" % (time.monotonic() - t, r.stats), flush=True)
        if a.v and r.ok:
            print("\n".join("    " + x for x in r.trace))
        if not r.ok:
            failed.append(seed)
    print("failed seeds: %s" % (failed or "none"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
