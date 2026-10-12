"""`lumix server start|stop|status`: run ./run.sh in the background with a pidfile."""

from __future__ import annotations

import os
import signal
import subprocess
import time
import urllib.parse
from typing import List, Optional

from .client import Client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _paths() -> "tuple[str, str, str]":
    run_sh = os.environ.get("LUMIX_RUN_SH") or os.path.join(ROOT, "run.sh")
    log_dir = os.environ.get("LUMIX_LOG_DIR") or os.path.join(ROOT, "logs")
    return run_sh, os.path.join(log_dir, "lumix-server.pid"), os.path.join(log_dir, "lumix-server.log")


def read_pid(pidfile: str) -> Optional[int]:
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    return pid


def status(client: Client) -> dict:
    _, pidfile, logfile = _paths()
    pid = read_pid(pidfile)
    up = client.reachable()
    return {"url": client.base, "reachable": up, "pid": pid, "pidfile": pidfile,
            "log": logfile, "managed": pid is not None}


def start(client: Client, extra: List[str], wait: float = 20.0) -> dict:
    run_sh, pidfile, logfile = _paths()
    if client.reachable():
        st = status(client)
        st["message"] = "already running at %s" % client.base
        return st
    pid = read_pid(pidfile)
    if pid is not None:
        raise RuntimeError("pid %d from %s is alive but %s does not answer; "
                           "`lumix server stop` first" % (pid, pidfile, client.base))
    args = list(extra)
    port = urllib.parse.urlsplit(client.base).port
    if port and port != 8770 and "--port" not in args:
        args += ["--port", str(port)]
    os.makedirs(os.path.dirname(pidfile), exist_ok=True)
    with open(logfile, "ab") as log:
        log.write(("\n=== lumix server start %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                         " ".join(args))).encode())
        log.flush()
        proc = subprocess.Popen([run_sh] + args, cwd=os.path.dirname(run_sh) or ".",
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
    with open(pidfile, "w") as f:
        f.write("%d\n" % proc.pid)
    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            os.unlink(pidfile)
            raise RuntimeError("server exited with code %s; see %s" % (proc.returncode, logfile))
        if client.reachable():
            st = status(client)
            st["message"] = "started pid %d, %s" % (proc.pid, client.base)
            return st
        time.sleep(0.3)
    st = status(client)
    st["message"] = "started pid %d but %s not answering yet; see %s" % (proc.pid, client.base, logfile)
    return st


def stop(client: Client, wait: float = 10.0) -> dict:
    _, pidfile, _ = _paths()
    pid = read_pid(pidfile)
    if pid is None:
        if os.path.exists(pidfile):
            os.unlink(pidfile)
        msg = "not running (no live pid in %s)" % pidfile
        if client.reachable():
            msg += "; a server not started by `lumix server start` answers at %s, stop it yourself" % client.base
        return {"stopped": False, "message": msg}
    # run.sh execs python, so the pid is the server; signal its group to be safe
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
        deadline = time.time() + wait
        while time.time() < deadline and read_pid(pidfile) is not None:
            _reap(pid)
            time.sleep(0.2)
        if read_pid(pidfile) is None:
            break
    os.unlink(pidfile)
    return {"stopped": True, "pid": pid, "message": "stopped pid %d" % pid}


def _reap(pid: int) -> None:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass

