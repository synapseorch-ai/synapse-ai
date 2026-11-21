"""`synapse stop` stops everything Synapse started, and only that, and says so in two lines (F1694).

It signalled only the pid in each pidfile. The frontend's is npm's, and npm left its web server running on the port, so
stop said "frontend stopped" while the app still answered; under a foreground start a stopped process stays a zombie
until its parent reaps it, and stop called it "Failed to stop". Now each process's whole tree is stopped, a zombie
counts as stopped, a pidfile whose pid now belongs to another program is never signalled, and stop says "Stopping
Synapse..." then one of "Synapse stopped.", "Synapse was not running." or "Could not stop: …".
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Windows stops a tree with taskkill /T")

#: npm as it behaves: starts its child (the web server), and on SIGTERM exits without stopping it.
NPM = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(120)
"""
#: A foreground `synapse start` as far as stop sees it: it starts npm and does not reap it while it waits.
SUPERVISOR = """
import subprocess, sys, time
npm = subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2]])
open(sys.argv[3], "w").write(str(npm.pid))
time.sleep(120)
"""


def _alive(pid: int) -> bool:
    import psutil

    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _wait_for(path: Path) -> int:
    for _ in range(100):
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError(f"{path} never written")


@pytest.fixture
def home(tmp_path, monkeypatch):
    from synapse import cli

    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path))
    (tmp_path / "run").mkdir()
    spawned: list[subprocess.Popen] = []
    yield {"dir": tmp_path, "cli": cli, "spawned": spawned}
    for proc in spawned:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            pass
        proc.kill()
        proc.wait()


def _stop(home, capsys) -> list[str]:
    home["cli"]._stop_command()
    return [line for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_a_detached_start_s_web_server_is_stopped_with_npm(home, capsys):
    server = home["dir"] / "server.pid"
    npm = subprocess.Popen([sys.executable, "-c", NPM, str(server)], start_new_session=True)
    home["spawned"].append(npm)
    web = _wait_for(server)
    home["cli"]._write_pidfile(home["cli"]._pid_file("frontend"), npm.pid)

    assert _stop(home, capsys) == ["Stopping Synapse...", "Synapse stopped."]
    npm.wait(timeout=10)
    assert not _alive(web)
    assert not home["cli"]._pid_file("frontend").exists()


def test_a_foreground_start_s_processes_are_stopped_and_said_to_be(home, capsys):
    npm_pid, server = home["dir"] / "npm.pid", home["dir"] / "server.pid"
    supervisor = subprocess.Popen([sys.executable, "-c", SUPERVISOR, NPM, str(server), str(npm_pid)],
                                  start_new_session=True)
    home["spawned"].append(supervisor)
    npm, web = _wait_for(npm_pid), _wait_for(server)
    home["cli"]._write_pidfile(home["cli"]._pid_file("frontend"), npm)

    # npm stays a zombie under its supervisor, which does not reap it: stopped, never "Failed to stop".
    assert _stop(home, capsys) == ["Stopping Synapse...", "Synapse stopped."]
    assert not _alive(npm) and not _alive(web)


def test_nothing_running_says_so(home, capsys):
    assert _stop(home, capsys) == ["Stopping Synapse...", "Synapse was not running."]


def test_a_pid_that_now_belongs_to_another_program_is_never_signalled(home, capsys):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    home["spawned"].append(other)
    pidfile = home["cli"]._pid_file("backend")
    home["cli"]._write_pidfile(pidfile, other.pid)
    an_hour_ago = time.time() - 3600
    os.utime(pidfile, (an_hour_ago, an_hour_ago))  # written long before that program started: a stale pidfile

    assert _stop(home, capsys) == ["Stopping Synapse...", "Synapse was not running."]
    assert other.poll() is None and _alive(other.pid)
    assert not pidfile.exists()


def test_what_would_not_stop_is_named_with_its_pid(home, capsys, monkeypatch):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    home["spawned"].append(sleeper)
    home["cli"]._write_pidfile(home["cli"]._pid_file("backend"), sleeper.pid)
    monkeypatch.setattr(home["cli"], "_stop_tree", lambda pid, timeout=5: [pid])

    assert _stop(home, capsys) == ["Stopping Synapse...", f"Could not stop: the backend (pid {sleeper.pid})."]
    assert home["cli"]._pid_file("backend").exists()
