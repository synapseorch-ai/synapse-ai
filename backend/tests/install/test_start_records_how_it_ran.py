"""`synapse start` records how it ran, so a later release can bring Synapse back the same way (F1641).

`run/state.json` held the ports a run used, its version and when it started; nothing said how it ran. A person's
`start` in a terminal, a `start --detach`, `--profile`, ports given, and autostart's own `start --detach --no-browser`
all looked the same afterwards, so an upgrade could not know whether or how to bring Synapse back (F1640 says it is
still stopped). Now the state holds `how`: the flags `start` was given (a port is null when it was not given, so a
restart picks one again) and who started it, "autostart" when the launcher marks its start (`SYNAPSE_STARTED_BY`),
else "person". `synapse status --json`, which the installer reads, reports it.

The systemd unit and the launchd plist mark their start. The Windows Run key is left exactly as it was (a launch we
cannot exercise here), so a Windows autostart reads "person" in 2.1. A mark rather than a flag: a downgraded Synapse
started by a newer launcher ignores it. This records only; restarting from it is a later release's.
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from synapse import cli
from synapse.installer import autostart

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path))
    monkeypatch.delenv("SYNAPSE_STARTED_BY", raising=False)
    cli._run_dir().mkdir(parents=True, exist_ok=True)  # where a start keeps its state
    return tmp_path


class TestHowItRan:
    def test_a_plain_start_by_a_person(self, home):
        assert cli._how_it_ran() == {"detached": False, "browser": True, "profile": False, "verbose": False,
                                     "backend_port": None, "frontend_port": None, "by": "person"}

    def test_each_flag_it_was_given(self, home):
        how = cli._how_it_ran(detach=True, no_browser=True, profile=True, verbose=True, backend_port=18765,
                              frontend_port=13001)
        assert how == {"detached": True, "browser": False, "profile": True, "verbose": True,
                       "backend_port": 18765, "frontend_port": 13001, "by": "person"}

    def test_a_start_its_launcher_marks_is_autostarts(self, home, monkeypatch):
        monkeypatch.setenv("SYNAPSE_STARTED_BY", "autostart")
        assert cli._how_it_ran(detach=True, no_browser=True)["by"] == "autostart"


class TestTheStateHoldsIt:
    def test_the_state_keeps_its_keys_and_adds_how(self, home):
        how = cli._how_it_ran(detach=True, no_browser=True)
        cli._write_state(18765, 13000, how)
        state = json.loads(cli._state_file().read_text())
        assert state["backend_port"] == 18765 and state["frontend_port"] == 13000
        assert state["version"] and state["started_at"] and state["how"] == how

    def test_start_writes_how_it_was_started(self):
        source = (ROOT / "synapse/cli.py").read_text(encoding="utf-8")
        assert "how = _how_it_ran(detach, no_browser, profile, verbose, backend_port, frontend_port)" in source
        assert "_write_state(effective_backend_port, effective_frontend_port, how)" in source

    def test_status_json_reports_it_and_reads_an_older_state(self, home, monkeypatch):
        monkeypatch.setattr(cli, "_login_is_on", lambda: True)
        cli._write_state(18765, 13000, cli._how_it_ran(detach=True))
        out = io.StringIO()
        with redirect_stdout(out):
            cli._status_command(as_json=True)
        assert json.loads(out.getvalue())["how"]["detached"] is True
        cli._state_file().write_text(json.dumps({"backend_port": 18765, "frontend_port": 13000}))
        out = io.StringIO()
        with redirect_stdout(out):
            cli._status_command(as_json=True)
        assert json.loads(out.getvalue())["how"] is None, "a start before F1641 says nothing of how"


class TestTheLaunchersMarkTheirStart:
    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the systemd unit")
    def test_the_systemd_unit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(autostart.subprocess, "run", lambda cmd, **kw: None)
        autostart.enable("/tools/synapse/bin/python", tmp_path / "home", [Path("/rt/node/bin")])
        unit = (tmp_path / "systemd" / "user" / "synapse-ai.service").read_text()
        assert "Environment=SYNAPSE_STARTED_BY=autostart" in unit, unit
        assert "ExecStart=/tools/synapse/bin/python -P -m synapse start --detach --no-browser" in unit

    def test_the_launchd_plist(self):
        source = (ROOT / "synapse/installer/autostart.py").read_text(encoding="utf-8")
        plist = source[source.index("<key>EnvironmentVariables</key>"):source.index("</plist>")]
        assert "<key>SYNAPSE_STARTED_BY</key>" in plist and "<string>autostart</string>" in plist, plist

    def test_the_windows_run_key_is_left_exactly_as_it_was(self):
        """A launch we cannot exercise here: its command line stays the same, byte for byte."""
        source = (ROOT / "synapse/installer/autostart.py").read_text(encoding="utf-8")
        windows = source[source.index("    if IS_WIN:\n        import subprocess as _sp"):source.index("        return\n")]
        assert windows == ("    if IS_WIN:\n        import subprocess as _sp\n        import winreg\n\n"
                           "        value = _sp.list2cmdline(cmd)\n"
                           "        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) "
                           "as key:\n            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, value)\n"), windows
        assert autostart.command(Path("/py")) == ["/py", "-P", "-m", "synapse", "start", "--detach", "--no-browser"]
