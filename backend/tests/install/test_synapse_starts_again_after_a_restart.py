"""Synapse starts again after a restart, unless the person says no (F1693).

The installer offers it with Yes as the default, asked or not, except where it cannot work or would surprise: a CI run,
a container, or no service manager. There it stays off and the install says why. An upgrade never changes the
person's choice. On Linux a systemd user unit starts at boot only while the user lingers, so turning it on asks
logind for that without a password, never with sudo, and records that it did; turning it off undoes only what it did.
The end of the install says when Synapse will start and how to turn that off.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from synapse.installer import autostart, flow

from test_installer_flow import _existing, world  # noqa: F401 — the installer's own test world


# ── turning it on and off, on Linux ───────────────────────────────────────────

@pytest.fixture
def linux(tmp_path, monkeypatch):
    """systemctl and loginctl as recorded calls, the unit under a temporary XDG folder, logind's linger folder."""
    monkeypatch.setattr(autostart, "IS_WIN", False)
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    lingering = tmp_path / "linger"
    lingering.mkdir()
    monkeypatch.setattr(autostart, "_LINGER_DIR", lingering)
    monkeypatch.setattr(autostart.getpass, "getuser", lambda: "pat")
    world = {"calls": [], "linger_allowed": True, "home": tmp_path / "home", "lingering": lingering}
    world["home"].mkdir()

    def run(cmd, *a, **kw):
        world["calls"].append(list(cmd))
        code = 0
        if cmd[:2] == ["loginctl", "enable-linger"]:
            if world["linger_allowed"]:
                (lingering / "pat").touch()
            else:
                code = 1
        elif cmd[:2] == ["loginctl", "disable-linger"]:
            (lingering / "pat").unlink(missing_ok=True)
        return subprocess.CompletedProcess(cmd, code, stdout="", stderr="")

    monkeypatch.setattr(autostart.subprocess, "run", run)
    return world


def _logind(calls):
    return [c for c in calls if c and c[0] == "loginctl"]


def test_turning_it_on_asks_logind_to_linger_without_a_password_and_records_that_it_did(linux):
    starts = autostart.enable("/opt/py/bin/python", linux["home"], [])
    assert starts.at_boot is True and not starts.hint
    assert _logind(linux["calls"]) == [["loginctl", "enable-linger", "--no-ask-password"]]
    assert not any("sudo" in part for call in linux["calls"] for part in call)
    assert json.loads((linux["home"] / "autostart.json").read_text())["linger_turned_on"] is True


def test_a_refused_linger_starts_it_at_log_in_and_says_the_sudo_line(linux):
    linux["linger_allowed"] = False
    starts = autostart.enable("/opt/py/bin/python", linux["home"], [])
    assert starts.at_boot is False
    assert starts.hint == "To start it at boot without logging in: sudo loginctl enable-linger pat"
    assert not (linux["home"] / "autostart.json").exists()
    assert not any(call[0] == "sudo" for call in linux["calls"])


def test_a_user_who_already_lingers_is_left_as_they_were(linux):
    (linux["lingering"] / "pat").touch()
    starts = autostart.enable("/opt/py/bin/python", linux["home"], [])
    assert starts.at_boot is True
    assert _logind(linux["calls"]) == []
    autostart.disable(linux["home"])
    assert _logind(linux["calls"]) == []
    assert (linux["lingering"] / "pat").exists()


def test_turning_it_off_ends_only_the_linger_it_turned_on(linux):
    autostart.enable("/opt/py/bin/python", linux["home"], [])
    autostart.disable(linux["home"])
    assert _logind(linux["calls"])[-1] == ["loginctl", "disable-linger", "--no-ask-password"]
    assert not (linux["lingering"] / "pat").exists()
    assert not (linux["home"] / "autostart.json").exists()


# ── the words ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("platform", "is_win", "question"), [
    ("linux", False, "Start Synapse when your computer starts?"),
    ("darwin", False, "Start Synapse when you log in?"),
    ("win32", True, "Start Synapse when you log in?"),
])
def test_the_question_says_when_it_starts_here(platform, is_win, question, monkeypatch):
    monkeypatch.setattr(autostart, "IS_WIN", is_win)
    monkeypatch.setattr(autostart.sys, "platform", platform)
    assert autostart.question() == question


@pytest.mark.parametrize(("at_boot", "first"), [
    (True, "Synapse starts again when this computer restarts."),
    (False, "Synapse starts again when you log in after a restart."),
])
def test_the_note_says_when_it_starts_and_how_to_turn_it_off(at_boot, first):
    lines = autostart.said(autostart.Starts(at_boot=at_boot))
    assert lines[0].startswith(first)
    assert "synapse autostart off" in lines[0]


def test_the_note_carries_the_sudo_line_when_linger_was_refused():
    hint = "To start it at boot without logging in: sudo loginctl enable-linger pat"
    assert autostart.said(autostart.Starts(at_boot=False, hint=hint))[-1] == hint


@pytest.mark.parametrize(("env", "container", "supported", "why"), [
    ({"CI": "true"}, False, True, "CI"),
    ({}, True, True, "container"),
    ({}, False, False, "systemd user session"),
])
def test_where_it_cannot_work_or_would_surprise_it_says_why(env, container, supported, why, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(autostart, "_in_container", lambda: container)
    monkeypatch.setattr(autostart, "supported", lambda: supported)
    monkeypatch.setattr(autostart, "IS_WIN", False)
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    assert why in (autostart.unavailable_reason() or "")


def test_a_machine_where_it_works_has_no_reason_against_it(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(autostart, "_in_container", lambda: False)
    monkeypatch.setattr(autostart, "supported", lambda: True)
    assert autostart.unavailable_reason() is None


# ── the install ────────────────────────────────────────────────────────────────

@pytest.fixture
def can_start(world, monkeypatch):  # noqa: F811 — the installer's world, where autostart can work
    turned_on = []
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(flow.autostart, "supported", lambda: True)
    monkeypatch.setattr(flow.autostart, "_in_container", lambda: False)

    def enable(python, home, dirs):
        turned_on.append(home)
        return autostart.Starts(at_boot=True)

    monkeypatch.setattr(flow.autostart, "enable", enable)
    world["turned_on"] = turned_on
    return world


def _applied(world) -> dict:  # noqa: F811
    return json.loads(Path(str(world["calls"]) + ".apply").read_text())["settings"]


def test_an_install_turns_it_on_by_default_and_says_how_to_turn_it_off(can_start):
    code, out, _ = can_start["install"]()
    assert code == 0, out
    assert _applied(can_start)["start_on_boot"] is True
    assert can_start["turned_on"] == [can_start["home"]]
    assert "Synapse starts again when this computer restarts." in out
    assert "synapse autostart off" in out


@pytest.mark.parametrize("where", ["ci", "container"])
def test_a_ci_run_or_a_container_keeps_it_off_and_says_why(where, can_start, monkeypatch):
    if where == "ci":
        monkeypatch.setenv("CI", "true")
    else:
        monkeypatch.setattr(flow.autostart, "_in_container", lambda: True)
    code, out, _ = can_start["install"]()
    assert code == 0, out
    assert _applied(can_start)["start_on_boot"] is False
    assert can_start["turned_on"] == []
    assert "will not start on its own after a restart" in out
    assert "synapse start" in out


def test_no_service_manager_keeps_it_off_and_says_why(world):  # noqa: F811 — supported() is False there
    code, out, _ = world["install"]()
    assert code == 0, out
    assert _applied(world)["start_on_boot"] is False
    assert "will not start on its own after a restart" in out


def test_an_upgrade_never_changes_the_persons_choice(can_start):
    _existing(can_start)
    code, out, _ = can_start["install"](upgrade=True, version_override="2.0.1")
    assert code == 0, out
    assert can_start["turned_on"] == []
    assert not Path(str(can_start["calls"]) + ".apply").exists()
    assert "synapse autostart off" not in out


# ── `synapse autostart` ───────────────────────────────────────────────────────

def test_autostart_on_and_off_say_the_same_as_the_install(monkeypatch, capsys, tmp_path):
    from synapse import cli

    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path))
    seen = []
    monkeypatch.setattr(autostart, "supported", lambda: True)
    monkeypatch.setattr(autostart, "enable", lambda python, home, dirs: autostart.Starts(at_boot=False, hint="H"))
    monkeypatch.setattr(autostart, "disable", lambda home=None: seen.append(home))
    cli._autostart_command("on")
    said = capsys.readouterr().out
    assert "Synapse starts again when you log in after a restart." in said and "H" in said
    cli._autostart_command("off")
    assert seen == [tmp_path]  # its own home, so it ends only the linger it turned on
    assert "no longer start on its own" in capsys.readouterr().out
