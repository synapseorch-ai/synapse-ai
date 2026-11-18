"""Stopping a foreground Synapse is said once, with one result line, and never signals Synapse itself (F1654).

`synapse start` in a terminal starts the backend and the web without a session of their own, so they share its
process group. Ctrl+C ran its shutdown, which stopped each child's tree by signalling the child's process group: its
own. That sent SIGTERM to Synapse itself, whose handler for SIGTERM is the same shutdown, so it ran again and said
"Stopping Synapse..." again, until the children were gone. Now a child that shares our own group has its tree stopped
by its pids, never by the group; and the shutdown runs once (a second signal while it runs is ignored), saying
"Stopping Synapse..." and then one result line.

Each case runs in a process of its own, in a session of its own, so no signal here can reach the test run.
Near miss: a child in a group of its own (a detached start's) is still stopped by its group, children and all.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

PRELUDE = f"""
import contextlib, io, json, os, signal, subprocess, sys, time
import psutil
sys.path.insert(0, {str(ROOT)!r})
from synapse import cli

SLEEPER = ("import subprocess, sys, time; "
           "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)")


def spawn(own_group=False):
    '''A child that starts a grandchild, as npm starts node; its output nowhere, so nothing waits on it.'''
    child = subprocess.Popen([sys.executable, "-c", SLEEPER], start_new_session=own_group,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    return child, [p.pid for p in psutil.Process(child.pid).children(recursive=True)]


def alive(pids):
    return any(psutil.pid_exists(p) and psutil.Process(p).status() != "zombie" for p in pids)
"""


def _isolated(body: str) -> dict:
    """Runs `body` in a session of its own and reads the JSON it prints last."""
    done = subprocess.run([sys.executable, "-c", PRELUDE + textwrap.dedent(body)], start_new_session=True,
                          capture_output=True, text=True, timeout=60)
    assert done.stdout.strip(), done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


STOP_ONE = """
    got = []
    signal.signal(signal.SIGTERM, lambda s, f: got.append(s))
    child, grandchildren = spawn(own_group={own})
    cli._kill_proc_tree(child, timeout=5)
    time.sleep(0.5)
    print(json.dumps({{"signalled_us": len(got), "child": child.poll() is None, "grandchild": alive(grandchildren)}}))
"""


def test_a_child_that_shares_our_group_is_stopped_without_signalling_us():
    said = _isolated(STOP_ONE.format(own=False))
    assert said == {"signalled_us": 0, "child": False, "grandchild": False}, said


def test_a_child_in_a_group_of_its_own_is_still_stopped_by_its_group():
    said = _isolated(STOP_ONE.format(own=True))
    assert said == {"signalled_us": 0, "child": False, "grandchild": False}, said


def test_ctrl_c_says_stopping_once_and_one_result_line():
    said = _isolated("""
        backend, _ = spawn()
        web, _ = spawn()
        shutdown = cli._shutdown_handler(web, backend)
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
        out, code = io.StringIO(), None
        with contextlib.redirect_stdout(out):
            try:
                os.kill(os.getpid(), signal.SIGINT)
                time.sleep(2)
            except SystemExit as e:
                code = e.code
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        print(json.dumps({"lines": lines, "code": code, "backend": backend.poll() is None,
                          "web": web.poll() is None}))
    """)
    assert said["lines"] == ["Stopping Synapse...", "Synapse stopped."], said
    assert said["code"] == 0 and not said["backend"] and not said["web"], said
