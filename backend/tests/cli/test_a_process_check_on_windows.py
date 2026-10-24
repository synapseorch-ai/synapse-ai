"""`synapse status` on Windows saw its own running backend and frontend as gone (F1036).

`_is_running` asked `os.kill(pid, 0)`. On Windows signal 0 is CTRL_C_EVENT, and
sending it to a detached process fails, so status reported `ready: false` while
both servers answered: Naveen's rc0 install ended "Starting failed: Synapse
started but does not answer yet". The same helper decides stop, start and
upgrade's restart. These tests run the Windows path by making `os.kill`
behave as it does there.
"""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading

import pytest

from synapse import cli


@pytest.fixture
def windows_kill(monkeypatch):
    """`os.kill(pid, 0)` as Windows does it to a detached process.

    psutil's own Linux check uses os.kill too; on Windows it asks the process
    table instead, so its calls go to the real os.kill.
    """
    real = os.kill

    def kill(pid, sig):
        if "psutil" in sys._getframe(1).f_code.co_filename:
            return real(pid, sig)
        if sig == 0:
            raise OSError(87, "The parameter is incorrect")   # GenerateConsoleCtrlEvent fails
        raise AssertionError(f"unexpected signal {sig}")

    monkeypatch.setattr(cli, "IS_WIN", True)
    monkeypatch.setattr(cli.os, "kill", kill)


def test_a_running_process_is_running_on_windows(windows_kill):
    assert cli._is_running(os.getpid()) is True


def test_a_finished_process_is_not_running(windows_kill):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert cli._is_running(child.pid) is False


@pytest.fixture
def frontend():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def test_status_says_ready_when_the_frontend_answers_on_windows(windows_kill, frontend, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_read_pidfile", lambda path: os.getpid())
    monkeypatch.setattr(cli, "_read_state", lambda: {"frontend_port": frontend})
    monkeypatch.setattr(cli, "_login_is_on", lambda: True)

    cli._status_command(as_json=True)

    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert report["backend"]["running"] and report["frontend"]["running"]
    assert report["ready"] is True
