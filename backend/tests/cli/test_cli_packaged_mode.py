"""
The CLI of a packaged install: its files under SYNAPSE_HOME, its servers quiet.

A pip, uv or npm install runs from a directory the package manager replaces on
upgrade, so the CLI puts everything it writes under SYNAPSE_HOME (~/.synapse).
`synapse start` sends the backend's and frontend's output to log files and
prints one line with the link; when a server does not come up it says which,
and shows the end of that server's log rather than five minutes of a counter.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

cli = pytest.importorskip("synapse.cli")

REPO = Path(__file__).resolve().parents[3]


# ── where things go ──────────────────────────────────────────────────────────

def test_a_checkout_is_not_packaged_and_site_packages_is(monkeypatch, tmp_path):
    assert cli._is_packaged() is False       # this repo, with its .git

    site = tmp_path / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    monkeypatch.setattr(cli, "ROOT_DIR", site)
    assert cli._is_packaged() is True

    (site / ".git").mkdir()
    assert cli._is_packaged() is False


def test_importing_the_packaged_cli_sets_synapse_home(tmp_path):
    """Set at import, before any path is read, so the backend inherits it."""
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    (site / "synapse").mkdir(parents=True)
    for name in ("__init__.py", "cli.py"):
        shutil.copy(REPO / "synapse" / name, site / "synapse" / name)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "me"),
           "USERPROFILE": str(tmp_path / "me"), "PYTHONPATH": str(site)}
    out = subprocess.run(
        [sys.executable, "-c",
         "import os, synapse.cli as c; print(os.environ['SYNAPSE_HOME']); "
         "print(c._env_file()); print(c._run_dir()); print(c._log_dir())"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    home = tmp_path / "me" / ".synapse"
    assert out.stdout.split() == [str(home), str(home / ".env"), str(home / "run"),
                                  str(home / "logs")]


def test_an_explicit_synapse_home_is_kept(tmp_path):
    site = tmp_path / "lib" / "site-packages"
    (site / "synapse").mkdir(parents=True)
    for name in ("__init__.py", "cli.py"):
        shutil.copy(REPO / "synapse" / name, site / "synapse" / name)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
           "PYTHONPATH": str(site), "SYNAPSE_HOME": str(tmp_path / "chosen")}
    out = subprocess.run(
        [sys.executable, "-c", "import synapse.cli as c; print(c._env_file())"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.stdout.strip() == str(tmp_path / "chosen" / ".env")


def test_a_checkout_keeps_its_own_env_run_and_logs(monkeypatch):
    monkeypatch.delenv("SYNAPSE_HOME", raising=False)
    assert cli._env_file() == cli.ROOT_DIR / ".env"
    # Its own run dir, so `synapse stop` in a checkout never stops a packaged
    # install on the same machine (both used ~/.synapse/run).
    assert cli._run_dir() == cli.BACKEND_DIR / "var" / "run"
    assert cli._log_dir() == cli.BACKEND_DIR / "var" / "logs"


def test_generated_secrets_go_to_synapse_homes_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path))
    monkeypatch.setenv("SYNAPSE_ACCESS_TOKEN", "x")
    monkeypatch.delenv("SYNAPSE_ACCESS_TOKEN")
    token = cli._ensure_access_token()
    assert f"SYNAPSE_ACCESS_TOKEN={token}" in (tmp_path / ".env").read_text()


def test_the_private_runtime_goes_first_on_path(monkeypatch, tmp_path):
    (tmp_path / "runtime" / "node" / ("" if cli.IS_WIN else "bin")).mkdir(parents=True)
    (tmp_path / "runtime" / "uv").mkdir()
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    cli._ensure_runtime_on_path()
    parts = os.environ["PATH"].split(os.pathsep)
    node_dir = tmp_path / "runtime" / "node"
    assert parts[:3] == [str(node_dir if cli.IS_WIN else node_dir / "bin"),
                         str(tmp_path / "runtime" / "uv"), "/usr/bin"]
    cli._ensure_runtime_on_path()   # idempotent
    assert os.environ["PATH"].split(os.pathsep) == parts


# ── the servers' output ──────────────────────────────────────────────────────

class _Proc:
    pid = 4242

    def poll(self):
        return None


@pytest.fixture
def popen_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path))
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _Proc()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    return calls


def test_the_backend_writes_to_its_log_not_the_terminal(popen_calls, tmp_path):
    cli.start_backend(port=9999)

    cmd, kwargs = popen_calls[0]
    assert kwargs["stdout"].name == str(tmp_path / "logs" / "backend.log")
    assert kwargs["stdout"].closed, "the CLI's copy of the handle is released"
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["env"]["PYTHONUNBUFFERED"] == "1"
    assert kwargs["env"]["SYNAPSE_BACKEND_PORT"] == "9999"
    assert "synapse start" in (tmp_path / "logs" / "backend.log").read_text()


def test_verbose_keeps_the_old_terminal_output(popen_calls):
    cli.start_backend(verbose=True)
    _, kwargs = popen_calls[0]
    assert "stdout" not in kwargs and "stderr" not in kwargs


@pytest.mark.skipif(os.name != "posix", reason="POSIX sessions")
def test_detached_services_get_their_own_session(popen_calls):
    cli.start_backend(detach=True)
    _, kwargs = popen_calls[0]
    assert kwargs["start_new_session"] is True
    assert "preexec_fn" not in kwargs


def test_a_big_log_is_rotated_at_start(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_LOG_MAX_BYTES", 10)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "backend.log").write_text("x" * 50)
    (logs / "backend.log.1").write_text("one")
    (logs / "backend.log.3").write_text("oldest")

    cli._rotate_log(logs / "backend.log")

    assert (logs / "backend.log.1").read_text() == "x" * 50
    assert (logs / "backend.log.2").read_text() == "one"
    # Nothing was in .2, so nothing moved onto .3.
    assert (logs / "backend.log.3").read_text() == "oldest"
    assert not (logs / "backend.log").exists()

    # Full: the oldest falls off the end rather than growing a fourth.
    (logs / "backend.log").write_text("y" * 50)
    cli._rotate_log(logs / "backend.log")
    assert (logs / "backend.log.3").read_text() == "one"
    assert not (logs / "backend.log.4").exists()


def test_a_small_log_is_left_alone(monkeypatch, tmp_path):
    (tmp_path / "backend.log").write_text("short")
    cli._rotate_log(tmp_path / "backend.log")
    assert (tmp_path / "backend.log").read_text() == "short"


# ── a start that fails ───────────────────────────────────────────────────────

@pytest.fixture
def start_env(monkeypatch, tmp_path):
    """`synapse start` with every side trip stubbed: prerequisites, browsers."""
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path))
    # `_start_command` writes the generated secrets into os.environ directly.
    # Set-then-delete makes monkeypatch restore their absence afterwards: a
    # leaked SYNAPSE_INTERNAL_TOKEN turns the token gate on for every later
    # test in the process (it cost 345 of them once).
    for var in ("SYNAPSE_INTERNAL_TOKEN", "SYNAPSE_JWT_SECRET", "SYNAPSE_ACCESS_TOKEN"):
        monkeypatch.setenv(var, "placeholder")
        monkeypatch.delenv(var)
    monkeypatch.setattr(cli, "check_prerequisites", lambda: None)
    monkeypatch.setattr(cli, "_ensure_playwright_browsers", lambda: None)
    monkeypatch.setattr(cli, "_ensure_coding_deps", lambda: None)
    monkeypatch.setattr(cli, "_effective_ports", lambda b=None, f=None: (18765, 13000))
    return tmp_path


def test_a_backend_that_crashes_is_reported_with_its_log(start_env, monkeypatch, capsys):
    def crashing_backend(detach=False, port=None, profile=False, verbose=False):
        return cli._popen(
            [sys.executable, "-c", "print('ImportError: no module named x'); raise SystemExit(3)"],
            start_env, dict(os.environ), cli._spawn_kwargs(detach, verbose, "backend"),
        )

    wizard_runs = []
    monkeypatch.setattr(cli, "start_backend", crashing_backend)
    monkeypatch.setattr("synapse._store.is_configured", lambda: False)
    monkeypatch.setattr("synapse.setup_wizard.run", lambda: wizard_runs.append(1))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))   # not a terminal

    with pytest.raises(SystemExit) as exit_info:
        cli._start_command(no_browser=True)

    out = capsys.readouterr().out
    assert exit_info.value.code == 1
    assert "Synapse did not start: the backend stopped (exit code 3)." in out
    assert "ImportError: no module named x" in out
    assert "synapse logs backend" in out
    assert not cli._pid_file("backend").exists()
    # No terminal, so no wizard: under a service manager it used to read EOF
    # and end the start with exit code 0.
    assert wizard_runs == []


# ── setup --apply, status --json, logs ───────────────────────────────────────

def _run(monkeypatch, capsys, argv, stdin=""):
    monkeypatch.setattr(sys, "argv", ["synapse", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    cli.main()
    return capsys.readouterr().out


def test_setup_apply_writes_known_keys_and_the_default_agent_once(monkeypatch, capsys):
    answers = {"settings": {"model": "claude-sonnet-4-6", "anthropic_key": "sk-ant-x",
                            "mode": "cloud", "frontend_port": 3001, "not_a_setting": 1},
               "default_agent": True}

    first = json.loads(_run(monkeypatch, capsys, ["setup", "--apply", "-"], json.dumps(answers)))
    second = json.loads(_run(monkeypatch, capsys, ["setup", "--apply", "-"], json.dumps(answers)))

    assert first == {"saved": ["anthropic_key", "frontend_port", "mode", "model"],
                     "ignored": ["not_a_setting"], "default_agent": "created"}
    assert second["default_agent"] == "exists"
    from synapse import _store
    stored = _store.load_settings()
    assert stored["model"] == "claude-sonnet-4-6" and stored["frontend_port"] == 3001
    assert "not_a_setting" not in stored


def test_status_json_when_nothing_runs(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path))
    monkeypatch.setenv("SYNAPSE_ACCESS_TOKEN", "tok123")
    monkeypatch.setattr(cli, "_effective_ports", lambda b=None, f=None: (8765, 3000))

    report = json.loads(_run(monkeypatch, capsys, ["status", "--json"]))

    assert report["backend"] == {"pid": None, "running": False}
    assert report["ready"] is False
    assert report["url"] == "http://localhost:3000"
    assert report["link"] == "http://localhost:3000/?token=tok123"


def test_status_json_reports_the_port_the_run_used(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path))
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "state.json").write_text(json.dumps({"frontend_port": 3005}))
    report = json.loads(_run(monkeypatch, capsys, ["status", "--json"]))
    assert report["url"] == "http://localhost:3005"


def test_autostart_on_registers_this_interpreter_and_records_it(monkeypatch, capsys, tmp_path):
    from synapse.installer import autostart

    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(autostart, "supported", lambda: True)
    monkeypatch.setattr(autostart, "enable", lambda python, home, dirs: calls.append(("on", python, home)))
    monkeypatch.setattr(autostart, "disable", lambda: calls.append(("off",)))
    monkeypatch.setattr(autostart, "is_on", lambda: bool(calls) and calls[-1][0] == "on")

    assert "will start when you log in" in _run(monkeypatch, capsys, ["autostart", "on"])
    assert calls == [("on", sys.executable, tmp_path)]
    assert _run(monkeypatch, capsys, ["autostart", "status"]).strip() == "on"
    from synapse import _store
    assert _store.load_settings()["start_on_boot"] is True
    _run(monkeypatch, capsys, ["autostart", "off"])
    assert _run(monkeypatch, capsys, ["autostart", "status"]).strip() == "off"


def test_autostart_says_so_where_it_cannot_work(monkeypatch, capsys):
    from synapse.installer import autostart

    monkeypatch.setattr(autostart, "supported", lambda: False)
    with pytest.raises(SystemExit):
        _run(monkeypatch, capsys, ["autostart", "on"])
    assert "no systemd user session" in capsys.readouterr().out


def test_logs_prints_the_tail(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path))
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "frontend.log").write_text("\n".join(f"l{i}" for i in range(100)))

    out = _run(monkeypatch, capsys, ["logs", "frontend", "-n", "3"])

    assert out.split() == ["l97", "l98", "l99"]
    assert "No backend log yet" in _run(monkeypatch, capsys, ["logs"])
