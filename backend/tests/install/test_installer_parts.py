"""
The installer's parts: terminal, progress, questions, what it finds, what it keeps.

The installer runs before Synapse exists and inside old v1.x venvs, so it may
import only the standard library — checked here first, because a third-party
import would pass every other test on a developer's machine and fail on the
first clean one.
"""
from __future__ import annotations

import ast
import io
import json
import os
import socket
import sqlite3
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PKG = REPO / "synapse" / "installer"
sys.path.insert(0, str(REPO))

from synapse.installer import (  # noqa: E402
    autostart, backup, home, legacy, ports, progress, providers, screen, tui, widgets,
)


# ── the release asset ────────────────────────────────────────────────────────

def test_the_pyz_runs_on_its_own_and_knows_its_version(tmp_path):
    import subprocess
    import zipfile

    build = subprocess.run([sys.executable, str(REPO / "scripts" / "build_installer.py"),
                            "--out", str(tmp_path), "--version", "9.8.7"],
                           capture_output=True, text=True, timeout=60)
    assert build.returncode == 0, build.stderr
    pyz = tmp_path / "synapse-install.pyz"
    with zipfile.ZipFile(pyz) as z:
        init = z.read("synapse/installer/__init__.py").decode()
    assert 'BUILT_FOR = "9.8.7"' in init
    digest = (tmp_path / "synapse-install.pyz.sha256").read_text().split()[0]
    import hashlib
    assert hashlib.sha256(pyz.read_bytes()).hexdigest() == digest

    # Run from an empty directory with no path to the repo: the zip is all it has.
    ran = subprocess.run([sys.executable, "-I", str(pyz), "--help"], cwd=tmp_path,
                         capture_output=True, text=True, timeout=60)
    assert ran.returncode == 0, ran.stderr
    assert "Install or upgrade Synapse" in ran.stdout


def test_a_background_failure_cancels_the_open_question():
    import threading

    cancel = threading.Event()
    cancel.set()
    s = screen.LiveScreen(tui.Term(stream=io.StringIO()), "t")
    asker = widgets.Asker(s, FakeKeys([]), cancel)
    with pytest.raises(widgets.Cancelled):
        asker.select("q", [widgets.Option("a", 1)])


# ── standard library only ────────────────────────────────────────────────────

def test_the_installer_imports_only_the_standard_library():
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    offenders = []
    for path in PKG.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            offenders += [f"{path.name}: {n}" for n in names if n.split(".")[0] not in allowed]
    assert offenders == []


# ── terminal ─────────────────────────────────────────────────────────────────

def test_a_pipe_gets_plain_output():
    term = tui.Term(stream=io.StringIO())
    assert term.live is False and term.color is False
    assert term.style("x", "bold") == "x"


def test_clip_keeps_colour_codes_and_counts_only_visible_columns():
    text = "\x1b[32m✓\x1b[0m " + "a" * 50
    assert tui.visible_len(text) == 52
    cut = tui.clip(text, 20, "…")
    assert tui.visible_len(cut) == 20
    assert cut.startswith("\x1b[32m✓\x1b[0m") and cut.endswith("…\x1b[0m")


def test_an_ascii_console_gets_ascii_glyphs():
    class Ascii(io.StringIO):
        encoding = "ascii"
    term = tui.Term(stream=Ascii())
    assert term.g("done") == "+" and term.g("ellipsis") == "..."


# ── progress ─────────────────────────────────────────────────────────────────

UV_OUTPUT = """Resolved 16 packages in 1.35s
Downloading cpython-3.12.8-linux-x86_64-gnu (32.9MiB)
Downloading pip (1.7MiB)
 Downloaded pip
 Downloaded cpython-3.12.8-linux-x86_64-gnu
Prepared 16 packages in 3.62s
Installed 16 packages in 30ms
 + certifi==2026.7.22
Installed 3 executables: http, httpie, https""".splitlines()


def test_uv_output_becomes_a_bar_that_only_goes_forward():
    parser = progress.UvInstall()
    seen = [parser.feed(line) for line in UV_OUTPUT]
    assert seen == sorted(seen)
    assert seen[0] == 0.05 and seen[-1] == 1.0
    assert parser.packages == 16
    # The big download moves the bar more than the small one.
    assert seen[4] - seen[3] > seen[3] - seen[2]


def test_an_unknown_line_changes_nothing():
    parser = progress.UvInstall()
    parser.feed("Resolved 3 packages in 0.1s")
    assert parser.feed("warning: something new") == 0.05


def test_playwright_parts_read_as_one_bar():
    parts = progress.Downloads(parts=2)
    values = [parts.feed(f"|■■■   | {p}% of 164.8 MiB") for p in (10, 50, 100)]
    values += [parts.feed(f"|■     | {p}% of 2.3 MiB") for p in (0, 100)]
    assert values == [0.05, 0.25, 0.5, 0.5, 1.0]


# ── the screen ───────────────────────────────────────────────────────────────

def test_the_plain_screen_prints_one_line_per_finished_step():
    out = io.StringIO()
    s = screen.make_screen(tui.Term(stream=out), "Synapse 2.0.0")
    s.add("a", "Getting Python")
    s.add("b", "Installing Synapse")
    s.open()
    s.start("a")
    s.done("a", "3.12.8")
    s.start("b")
    s.fail("b", "boom")
    lines = out.getvalue().splitlines()
    assert lines[0] == "Synapse 2.0.0"
    assert lines[1].startswith("[1/2] Getting Python ") and lines[1].endswith(" ok  3.12.8")
    assert lines[2].startswith("[2/2] Installing Synapse") and "FAILED  boom" in lines[2]


def test_the_live_frame_draws_steps_and_the_question():
    s = screen.LiveScreen(tui.Term(stream=io.StringIO()), "Synapse 2.0.0")
    s.add("a", "Getting Python")
    s.add("b", "Installing Synapse")
    s.done("a", "3.12.8")
    s.start("b")
    s.progress("b", 0.5)
    s.set_panel(["  Which AI?"])
    text = "\n".join(s.lines())
    assert "Getting Python" in text and "3.12.8" in text
    assert "%" in text and "Which AI?" in text


# ── questions ────────────────────────────────────────────────────────────────

class FakeKeys(tui.Keys):
    def __init__(self, keys):
        self.keys = list(keys)

    def read(self):
        return self.keys.pop(0)


def _asker(keys):
    s = screen.LiveScreen(tui.Term(stream=io.StringIO()), "t")
    return widgets.Asker(s, FakeKeys(keys) if keys is not None else None)


def test_without_a_keyboard_every_question_takes_its_default():
    asker = _asker(None)
    opts = [widgets.Option("a", 1), widgets.Option("b", 2)]
    assert asker.select("q", opts, default=1) == 2
    assert asker.text("q", default="x") == "x"
    assert asker.confirm("q", default=False) is False


def test_select_moves_filters_and_goes_back():
    opts = [widgets.Option(n, n) for n in ("alpha", "beta", "gamma")]
    assert _asker(["down", "enter"]).select("q", opts) == "beta"
    assert _asker(["up", "enter"]).select("q", opts) == "gamma"
    assert _asker(["3", "enter"]).select("q", opts) == "gamma"
    assert _asker(["g", "a", "enter"]).select("q", opts, filterable=True) == "gamma"
    assert _asker(["esc"]).select("q", opts, back=True) is widgets.BACK


def test_a_secret_is_typed_hidden_and_validated():
    asker = _asker(list("sk-1") + ["enter"] + list("234") + ["enter"])
    answer = asker.text("key", secret=True,
                        validate=lambda v: "too short" if len(v) < 5 else None)
    assert answer == "sk-1234"


def test_the_provider_question_checks_the_key_by_listing_models(monkeypatch):
    calls = []

    def lister(key):
        calls.append(key)
        if key == "bad":
            raise providers.net.HttpError("401", 401)
        return ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6"]

    monkeypatch.setitem(providers.CLOUDS, "anthropic", providers.Cloud(
        "anthropic", "Anthropic", "anthropic_key", lister, "claude-sonnet-4-6"))
    found = providers.Found(ollama=[], claude=False, gemini=False, codex=False)
    # Anthropic, a key it refuses, a key it takes, then the preferred model.
    keys = ["enter"] + list("bad") + ["enter"] + list("good") + ["enter", "enter"]
    answer = providers.ask(_asker(keys), found)
    assert calls == ["bad", "good"]
    assert answer == {"anthropic_key": "good", "model": "claude-sonnet-4-6", "mode": "cloud"}


def test_ollama_is_offered_first_when_it_is_here_and_stored_with_its_prefix():
    found = providers.Found(ollama=["gemma3:4b", "llama3:8b"], claude=False, gemini=False, codex=False)
    answer = providers.ask(_asker(["enter", "enter"]), found)
    # Bare, `gemma3` routed to Gemini.
    assert answer == {"model": "ollama.gemma3:4b", "mode": "local"}


def test_skip_and_no_keyboard_both_mean_no_answer():
    found = providers.Found(ollama=[], claude=True, gemini=False, codex=False)
    assert providers.ask(_asker(None), found) == {}


def test_grok_writes_the_key_the_engine_reads():
    assert providers.CLOUDS["grok"].key_setting == "grok_key"


def test_the_preferred_model_skips_minis_and_previews():
    models = ["gpt-5-mini", "gpt-5-preview", "gpt-5", "gpt-4o"]
    assert models[providers.preferred_index("openai", models)] == "gpt-5"


# ── finding v1.x ─────────────────────────────────────────────────────────────

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(legacy.shutil, "which", lambda *a, **k: None)
    return tmp_path


def _v19_script_install(root: Path) -> Path:
    (root / "backend" / "data" / "chat_sessions").mkdir(parents=True)
    (root / "backend" / "logs" / "orchestration_logs").mkdir(parents=True)
    (root / "synapse").mkdir()
    (root / "bin").mkdir()
    (root / ".installed").write_text("{}")
    (root / "pyproject.toml").write_text('[project]\nname = "synapse-orch-ai"\nversion = "1.9.0"\n')
    (root / ".env").write_text("SYNAPSE_DATA_DIR=backend/data\nSYNAPSE_JWT_SECRET=keep\n")
    data = root / "backend" / "data"
    (data / "user_agents.json").write_text(json.dumps([{"id": "a1"}, {"id": "a2"}]))
    (data / "orchestrations.json").write_text(json.dumps([{"id": "o1"}]))
    (data / "settings.json").write_text(json.dumps({"model": "claude-sonnet-4-6"}))
    (data / "chat_sessions" / "a1_s1.json").write_text("{}")
    (root / "backend" / "logs" / "orchestration_logs" / "r1.log").write_text("x")
    return root


def _script_root(home_dir: Path) -> Path:
    if sys.platform == "darwin":
        return home_dir / "Library" / "Application Support" / "SynapseAI"
    if sys.platform == "win32":
        return home_dir / "AppData" / "Local" / "Programs" / "SynapseAI"
    return home_dir / ".local" / "share" / "SynapseAI"


def test_a_v19_script_install_is_found_with_what_it_holds(fake_home):
    root = _v19_script_install(_script_root(fake_home))

    m = legacy.find(fake_home / ".synapse")

    assert [i.kind for i in m.installs] == ["script"] and m.version == "1.9.0"
    assert m.data_dirs == [root / "backend" / "data"]
    assert m.logs_dirs == [root / "backend" / "logs"]
    assert m.counts["agents"] == 2 and m.counts["orchestrations"] == 1
    assert m.counts["chats"] == 1 and m.counts["runs"] == 1
    assert m.has_model()
    assert legacy.summary(m.counts)[:2] == ["2 agents", "1 orchestration"]
    assert root in m.leftovers


def test_a_checkout_and_a_v2_carrier_are_not_installs(fake_home):
    carrier = _script_root(fake_home)
    (carrier / "backend").mkdir(parents=True)
    (carrier / "pyproject.toml").write_text('version = "2.0.0"\n')
    assert legacy.find(fake_home / ".synapse").installs == []

    (carrier / ".installed").write_text("{}")
    (carrier / ".git").mkdir()
    assert legacy.find(fake_home / ".synapse").installs == []


def test_an_npm_install_is_found_by_its_venv_and_data(fake_home):
    home_dir = fake_home / ".synapse"
    (home_dir / "venv").mkdir(parents=True)
    (home_dir / "data").mkdir()
    (home_dir / "data" / "settings.json").write_text("{}")

    m = legacy.find(home_dir)

    assert [i.kind for i in m.installs] == ["npm"]
    assert m.data_dirs == [home_dir / "data"]
    assert home_dir / "venv" in m.leftovers


def test_nothing_found_on_a_clean_machine(fake_home):
    assert legacy.find(fake_home / ".synapse").found is False


def test_definitions_naming_the_old_folder_are_found(tmp_path):
    root = str(tmp_path / "SynapseAI")
    data = tmp_path / "data"
    data.mkdir()
    (data / "mcp_servers.json").write_text(json.dumps([
        {"name": "local", "command": f"{root}/backend/venv/bin/python", "args": ["x.py"]},
        {"name": "remote", "url": "https://mcp.example"},
    ]))
    (data / "repos.json").write_text(json.dumps([{"id": "r1", "name": "Mine", "path": "/src/mine"}]))
    windows_root = "C:\\Users\\me\\AppData\\Local\\Programs\\SynapseAI"
    (data / "user_agents.json").write_text(json.dumps([{"id": "a", "name": "Win", "cwd": windows_root}]))

    assert legacy.mentions([data], [root, windows_root]) == ["agent Win", "MCP server local"]
    assert legacy.mentions([data], []) == []


def test_the_old_env_keeps_its_secrets_but_not_its_paths(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SYNAPSE_DATA_DIR=backend/data\nSYNAPSE_JWT_SECRET=s\n"
                   "SYNAPSE_INTERNAL_TOKEN=t\nNEXT_PUBLIC_BACKEND_PORT=8765\n# c\n")
    assert legacy.merged_env([env]) == {"SYNAPSE_JWT_SECRET": "s", "SYNAPSE_INTERNAL_TOKEN": "t"}


def test_the_old_path_lines_go_and_nothing_else(fake_home):
    bashrc = fake_home / ".bashrc"
    old_bin = fake_home / ".local" / "share" / "SynapseAI" / "bin"
    bashrc.write_text(f"alias ll='ls -l'\n\n# Synapse AI\nexport PATH=\"{old_bin}:$PATH\"\n"
                      f"export PATH=\"$HOME/go/bin:$PATH\"\n")

    changed = legacy.remove_path_lines([old_bin])

    assert changed == [bashrc]
    assert bashrc.read_text() == "alias ll='ls -l'\n\nexport PATH=\"$HOME/go/bin:$PATH\"\n"


def test_a_pid_file_naming_someone_elses_process_is_not_killed(tmp_path, monkeypatch):
    pid_file = tmp_path / "backend.pid"
    pid_file.write_text(str(os.getpid()))    # this test process: not Synapse
    monkeypatch.setattr(legacy, "_cmdline", lambda pid: "python -m pytest")
    killed = []
    monkeypatch.setattr(legacy, "_terminate", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(legacy, "_stop_service_managers", lambda log=None: None)

    assert legacy.stop_services(legacy.Migration(pid_files=[pid_file])) == []
    assert killed == []


# ── backups ──────────────────────────────────────────────────────────────────

def test_a_folder_backup_is_private_complete_and_read_back(tmp_path):
    data = tmp_path / "data"
    (data / "vault").mkdir(parents=True)
    (data / "chroma_db").mkdir()
    (data / "user_agents.json").write_text("[]")
    (data / "vault" / "a.md").write_text("a")
    (data / "chroma_db" / "big.bin").write_text("x" * 1000)
    (data / "backend.pid").write_text("123")
    env = tmp_path / ".env"
    env.write_text("K=v")

    archive = backup.backup_folders({"data-1": data, "env-1/.env": env}, tmp_path / "backups", "1.9.0")

    assert archive.name.startswith("synapse-1.9.0-") and archive.name.endswith(".tar.gz")
    if os.name == "posix":
        assert archive.stat().st_mode & 0o077 == 0
    with tarfile.open(archive) as t:
        names = sorted(t.getnames())
        manifest = json.loads(t.extractfile("manifest.json").read())
    assert names == ["data-1/user_agents.json", "data-1/vault/a.md", "env-1/.env", "manifest.json"]
    assert manifest["sources"]["data-1"] == str(data)


def test_a_sqlite_backup_is_a_working_database(tmp_path):
    db = tmp_path / "synapse.db"
    with sqlite3.connect(db) as c:
        c.execute("create table t (x)")
        c.execute("insert into t values (42)")

    copy = backup.backup_sqlite(db, tmp_path / "backups", "2.0.0")

    with sqlite3.connect(copy) as c:
        assert c.execute("select x from t").fetchone() == (42,)


def test_only_the_newest_backups_are_kept(tmp_path):
    for i in range(7):
        f = tmp_path / f"synapse-x-{i}.db"
        f.write_text("")
        os.utime(f, (i, i))
    backup._prune(tmp_path, "*.db")
    assert sorted(p.name for p in tmp_path.glob("*.db")) == [f"synapse-x-{i}.db" for i in range(2, 7)]


# ── autostart, ports, home ───────────────────────────────────────────────────

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the systemd unit")
def test_the_systemd_unit_runs_this_installs_python_detached(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    ran = []
    monkeypatch.setattr(autostart.subprocess, "run", lambda cmd, **kw: ran.append(cmd))

    autostart.enable("/tools/synapse/bin/python", tmp_path / "home", [Path("/rt/node/bin")])

    unit = (tmp_path / "systemd" / "user" / "synapse-ai.service").read_text()
    assert "ExecStart=/tools/synapse/bin/python -P -m synapse start --detach --no-browser" in unit
    assert "ExecStop=/tools/synapse/bin/python -P -m synapse stop" in unit
    assert "Type=oneshot" in unit and "TimeoutStartSec=600" in unit
    assert f"SYNAPSE_HOME={tmp_path / 'home'}" in unit
    assert ["systemctl", "--user", "enable", "synapse-ai.service"] in ran
    assert autostart.is_on()
    autostart.disable()
    assert not autostart.is_on()


def test_a_taken_port_is_skipped():
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        s.listen()
        taken = s.getsockname()[1]
        assert ports.is_free(taken) is False
        assert ports.free_port(taken) > taken


def test_install_json_merges_and_forgets(tmp_path):
    home.write_install(tmp_path, version="2.0.0", method="uv")
    data = home.write_install(tmp_path, version="2.0.1", method=None)
    assert data["version"] == "2.0.1" and "method" not in data
    assert home.read_install(tmp_path)["version"] == "2.0.1"


def test_a_second_installer_is_refused_while_the_first_runs(tmp_path):
    (tmp_path / "install.lock").write_text(str(os.getppid()))   # alive: our parent
    with pytest.raises(home.Busy):
        with home.InstallLock(tmp_path):
            pass
    (tmp_path / "install.lock").write_text("999999")            # a dead installer's
    with home.InstallLock(tmp_path):
        pass
    assert not (tmp_path / "install.lock").exists()
