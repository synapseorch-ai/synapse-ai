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


def test_the_download_says_megabytes_and_the_time_left():
    """~400 MB took Naveen ~20 minutes on rc0 with only a bar (F1037)."""
    now = [100.0]
    parser = progress.UvInstall(clock=lambda: now[0])
    parser.feed("Resolved 180 packages in 3.06s")
    parser.feed("Downloading scipy (300.0MiB)")
    parser.feed("Downloading numpy (100.0MiB)")
    assert parser.detail() is None                # nothing finished: no rate yet
    now[0] = 220.0
    parser.feed(" Downloaded numpy")              # 100 MB in 2 min: 300 MB is 6 min
    assert parser.detail() == "100 of 400 MB · about 6 min left"
    parser.feed(" Downloaded scipy")
    parser.feed("Prepared 180 packages in 5m")
    assert parser.detail() == ""                  # the downloads are over


def test_the_last_minute_says_so():
    now = [0.0]
    parser = progress.UvInstall(clock=lambda: now[0])
    parser.feed("Downloading a (10.0MiB)")
    parser.feed("Downloading b (1.0MiB)")
    now[0] = 50.0
    parser.feed(" Downloaded a")
    assert parser.detail() == "10 of 11 MB · under a minute left"


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
    # Anthropic, a key it refuses, a key it takes, then the preferred model;
    # no other key, and Synapse AI's model the same (ONB1).
    keys = ["enter"] + list("bad") + ["enter"] + list("good") + ["enter", "enter"] + ["enter", "enter"]
    answer = providers.ask(_asker(keys), found)
    assert calls == ["bad", "good"]
    assert answer == {"anthropic_key": "good", "model": "claude-sonnet-4-6", "mode": "cloud"}


def test_ollama_is_offered_first_when_it_is_here_and_stored_with_its_prefix():
    found = providers.Found(ollama=["gemma3:4b", "llama3:8b"], claude=False, gemini=False, codex=False)
    answer = providers.ask(_asker(["enter", "enter", "enter", "enter"]), found)
    # Bare, `gemma3` routed to Gemini.
    assert answer == {"model": "ollama.gemma3:4b", "mode": "local"}


def test_skip_and_no_keyboard_both_mean_no_answer():
    found = providers.Found(ollama=[], claude=True, gemini=False, codex=False)
    assert providers.ask(_asker(None), found) == {}


def _two_clouds(monkeypatch, calls=None):
    def anthropic(key):
        if calls is not None:
            calls.append(("anthropic", key))
        if key == "bad":
            raise providers.net.HttpError("401", 401)
        return ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6"]

    def openai(key):
        if calls is not None:
            calls.append(("openai", key))
        if key == "bad":
            raise providers.net.HttpError("401", 401)
        return ["gpt-5", "gpt-4.1-mini"]

    monkeypatch.setitem(providers.CLOUDS, "anthropic", providers.Cloud(
        "anthropic", "Anthropic", "anthropic_key", anthropic, "claude-sonnet-4-6"))
    monkeypatch.setitem(providers.CLOUDS, "openai", providers.Cloud(
        "openai", "OpenAI", "openai_key", openai, "gpt-4o"))
    return providers.Found(ollama=[], claude=False, gemini=False, codex=False)


def test_a_second_key_then_the_everyday_model_and_synapse_ais(monkeypatch):
    """ONB1 (D295, F723): keys for more than one provider, then two models —
    the everyday one (workflows, agents, chat) and Synapse AI's."""
    calls = []
    found = _two_clouds(monkeypatch, calls)
    keys = (
        ["enter"] + list("good") + ["enter"]   # Anthropic, its key
        + ["enter"]                            # its preferred model (Sonnet)
        + ["1", "enter"]                       # add another key? Yes
        + ["enter"]                            # OpenAI, the first provider not yet keyed
        + list("bad") + ["enter"]              # a key it refuses
        + list("ok") + ["enter"]               # a key it takes
        + ["enter"]                            # another? No
        + ["enter"]                            # the everyday model: Sonnet stays
        + ["5", "enter"]                       # Synapse AI's: Same, haiku, sonnet, opus, gpt-5
    )
    answer = providers.ask(_asker(keys), found)
    assert calls == [("anthropic", "good"), ("openai", "bad"), ("openai", "ok")]
    assert answer == {"anthropic_key": "good", "openai_key": "ok", "model": "claude-sonnet-4-6",
                      "builder_model": "gpt-5", "mode": "cloud"}
    assert providers.describe(answer) == "claude-sonnet-4-6, Synapse AI on gpt-5"


def test_the_everyday_model_can_come_from_the_second_key(monkeypatch):
    found = _two_clouds(monkeypatch)
    keys = (
        ["enter"] + list("good") + ["enter", "enter"]   # Anthropic, Sonnet
        + ["1", "enter", "enter"] + list("ok") + ["enter"]   # OpenAI's key
        + ["enter"]                                     # no more
        + ["4", "enter"]                                # everyday: haiku, sonnet, opus, gpt-5
        + ["enter"]                                     # Synapse AI's: the same
    )
    answer = providers.ask(_asker(keys), found)
    assert answer["model"] == "gpt-5"
    assert "builder_model" not in answer, "the same model is the default: nothing to write"


def test_a_local_model_still_asks_for_synapse_ais(monkeypatch):
    found = providers.Found(ollama=["gemma3:4b", "llama3:8b"], claude=False, gemini=False, codex=False)
    # Ollama's first model; no key; Synapse AI's model: the second option is that same list's first.
    answer = providers.ask(_asker(["enter", "enter", "enter", "3", "enter"]), found)
    assert answer == {"model": "ollama.gemma3:4b", "builder_model": "ollama.llama3:8b", "mode": "local"}


def test_going_back_from_the_second_provider_asks_again(monkeypatch):
    found = _two_clouds(monkeypatch)
    keys = (
        ["enter"] + list("good") + ["enter", "enter"]
        + ["1", "enter", "esc"]    # Yes, then back out of the provider list
        + ["enter"]                # asked again: No
        + ["enter"]                # Synapse AI's: the same
    )
    answer = providers.ask(_asker(keys), found)
    assert answer == {"anthropic_key": "good", "model": "claude-sonnet-4-6", "mode": "cloud"}


def test_synapse_setup_asks_the_installers_question(monkeypatch):
    """ONB1 (F724): `synapse setup` asked its own eight-provider question (no
    hosted providers, a bare Ollama name); it asks the installer's now, and saves
    what that answers."""
    from synapse import setup_wizard

    asked, saved = [], {}
    monkeypatch.setattr(setup_wizard, "load_settings", lambda: dict(setup_wizard.DEFAULT_SETTINGS))
    monkeypatch.setattr(setup_wizard, "save_settings", saved.update)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr(providers, "ask", lambda asker, found=None: asked.append(asker) or {
        "groq_key": "gk", "model": "groq.openai/gpt-oss-120b", "builder_model": "groq.qwen3", "mode": "cloud"})
    setup_wizard.run()
    assert len(asked) == 1
    assert saved["groq_key"] == "gk" and saved["model"] == "groq.openai/gpt-oss-120b"
    assert saved["builder_model"] == "groq.qwen3" and saved["mode"] == "cloud"
    assert saved["backend_port"] == 8765, "the ports are still asked, and kept on Enter"


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


def _cloned_v1(root: Path, marker: bool = True) -> Path:
    """What v1.x's setup left when the release download failed: a git clone."""
    (root / ".git").mkdir(parents=True)
    (root / "backend" / "data").mkdir(parents=True)
    (root / "backend" / "data" / "settings.json").write_text('{"model": "gemini.pro"}')
    (root / "pyproject.toml").write_text('version = "1.7.4"\n')
    if marker:
        (root / ".installed").write_text("{}")
    return root


def test_a_v1_install_setup_cloned_is_found(fake_home):
    """v1.x's setup fell back to `git clone` when the release download failed (F1057)."""
    root = _cloned_v1(_script_root(fake_home))

    m = legacy.find(fake_home / ".synapse")

    assert [(i.kind, i.root, i.version) for i in m.installs] == [("script", root, "1.7.4")]
    assert m.data_dirs == [root / "backend" / "data"]
    assert root in m.leftovers


def test_a_v1_checkout_nobody_installed_is_still_skipped(fake_home):
    _cloned_v1(_script_root(fake_home), marker=False)
    assert legacy.find(fake_home / ".synapse").installs == []


def test_a_cloned_v1_install_is_found_where_windows_put_it(fake_home, monkeypatch):
    """Naveen's: %LOCALAPPDATA%\\Programs\\SynapseAI, with .git, .installed and data."""
    monkeypatch.setattr(legacy, "IS_WIN", True)
    root = _cloned_v1(fake_home / "AppData" / "Local" / "Programs" / "SynapseAI")

    m = legacy.find(fake_home / ".synapse")

    assert [(i.kind, i.root, i.bin_dir) for i in m.installs] == [("script", root, root / "bin")]
    assert m.has_model()


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


def _old(root: Path) -> legacy.Migration:
    return legacy.Migration(installs=[legacy.OldInstall("script", root, "1.9.3")])


def _listener(cwd: Path, script: str, port_file: Path):
    """A stand-in for the old frontend: an interpreter running a script under `cwd`, listening on a port,
    started in its own session as `synapse start --detach` starts it, and reaped when it ends."""
    import subprocess
    import threading
    import time

    (cwd / Path(script).parent).mkdir(parents=True, exist_ok=True)
    (cwd / script).write_text(
        "import socket, sys, time\n"
        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
        f"open({str(port_file)!r}, 'w').write(str(s.getsockname()[1]))\n"
        "time.sleep(120)\n")
    proc = subprocess.Popen([sys.executable, script, "start"], cwd=cwd, start_new_session=True)
    threading.Thread(target=proc.wait, daemon=True).start()
    for _ in range(100):
        if port_file.exists() and port_file.read_text():
            return proc, int(port_file.read_text())
        time.sleep(0.05)
    proc.kill()
    raise AssertionError("the listener never started")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_an_old_install_process_without_a_pid_file_is_stopped(tmp_path, monkeypatch):
    """F1350: a 1.x `synapse upgrade` stops its own services and removes their PID files before it hands over,
    but its frontend's web server lives on and keeps the old address. The hand-off finds it by what it runs."""
    root = tmp_path / "SynapseAI"
    proc, port = _listener(root / "frontend", "scripts/load-env.py", tmp_path / "port")
    monkeypatch.setattr(legacy, "_stop_service_managers", lambda log=None: None)
    m = _old(root)
    try:
        stopped = legacy.stop_services(m)
        assert proc.pid in stopped
        assert proc.wait(timeout=10) is not None
        assert ports.is_free(port)
        python = os.path.basename(sys.executable)
        assert m.stopped_words == [f"the old web page ({python} … load-env.py start, pid {proc.pid})"]
    finally:
        proc.kill()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_a_process_outside_the_old_folder_is_left_running(tmp_path, monkeypatch):
    root = tmp_path / "SynapseAI"
    root.mkdir()
    proc, port = _listener(tmp_path / "elsewhere", "serve.py", tmp_path / "port")
    monkeypatch.setattr(legacy, "_stop_service_managers", lambda log=None: None)
    try:
        assert legacy.stop_services(_old(root)) == []
        assert proc.poll() is None and not ports.is_free(port)
    finally:
        proc.kill()


def test_only_what_runs_the_old_code_is_picked_never_a_shell_or_editor_there(tmp_path):
    """F1350 (eb's pins): a person's shell, editor or `tail` in the old folder is theirs; what runs the old code is
    picked, with what it started (next-server retitles itself and names no path), listeners first."""
    root = tmp_path / "SynapseAI"
    r, fe = str(root), str(root / "frontend")
    procs = [
        legacy.Proc(101, 1, ["-bash"], "/usr/bin/bash", r),
        legacy.Proc(102, 1, ["vim", f"{r}/.env"], "/usr/bin/vim", r),
        legacy.Proc(103, 1, ["tail", "-f", f"{r}/backend/logs/x.log"], "/usr/bin/tail", r),
        legacy.Proc(104, 1, ["python3", "-m", "http.server"], "/usr/bin/python3.12", r),
        legacy.Proc(105, 1, ["node", "scripts/load-env.js", "start"], "/usr/bin/node", fe),
        legacy.Proc(106, 105, ["next-server (v16.2.6)"], "/usr/bin/node", fe, ports=(18872,)),
        legacy.Proc(107, 1, [f"{r}/backend/venv/bin/python", "main.py"], "/usr/bin/python3.12", f"{r}/backend",
                    ports=(18871,)),
        legacy.Proc(108, 1, ["node", "/home/x/other/server.js"], "/usr/bin/node", r),
    ]

    picked = legacy.old_install_processes(_old(root), procs)

    assert [p.pid for p in picked] == [106, 107, 105]
    assert legacy.describe(procs[4]) == "the old web page (node … load-env.js start, pid 105)"
    assert legacy.describe(procs[5]) == "the old web page (next-server (v16.2.6), pid 106)"
    assert legacy.describe(procs[6]) == "the old backend (python … main.py, pid 107)"


def test_the_handoff_never_stops_itself_its_parents_or_another_users(tmp_path):
    """The hand-off runs from inside the old folder, under the old install's own `synapse upgrade`."""
    root = tmp_path / "SynapseAI"
    old_python = f"{root}/backend/venv/bin/python"
    me, parent = os.getpid(), os.getppid()
    other_uid = (os.getuid() + 1) if hasattr(os, "getuid") else None
    procs = [
        legacy.Proc(parent, 1, [old_python, f"{root}/synapse/cli.py", "upgrade"]),
        legacy.Proc(me, parent, [old_python, f"{root}/synapse/cli.py", "upgrade"]),
        legacy.Proc(9001, 1, [old_python, "main.py"], cwd=f"{root}/backend", uid=other_uid),
    ]

    picked = legacy.old_install_processes(_old(root), procs)

    assert [p.pid for p in picked] == ([] if other_uid is not None else [9001])


def test_a_pip_install_is_its_own_packages_never_all_of_site_packages(tmp_path):
    site = tmp_path / "site-packages"
    m = legacy.Migration(installs=[legacy.OldInstall("pip", site, "1.9.3")])
    procs = [
        legacy.Proc(201, 1, ["node", f"{site}/synapse/_frontend/server.js"], "/usr/bin/node"),
        legacy.Proc(202, 1, ["python3", f"{site}/someone_else/app.py"], "/usr/bin/python3"),
    ]

    assert [p.pid for p in legacy.old_install_processes(m, procs)] == [201]


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


# ── options from the environment: `irm … | iex` passes no arguments (IU1) ─────

@pytest.fixture
def captured_options(monkeypatch):
    from synapse.installer import flow

    seen = []

    class Fake:
        def __init__(self, options, term=None):
            seen.append(options)

        def run(self):
            return 0

    monkeypatch.setattr(flow, "Installer", Fake)
    for var in ("SYNAPSE_YES", "SYNAPSE_NO_START", "SYNAPSE_NO_BROWSER", "SYNAPSE_INSTALL_VERSION",
                "SYNAPSE_WITH"):
        monkeypatch.delenv(var, raising=False)
    return seen


def test_every_user_option_has_an_environment_twin(captured_options, monkeypatch):
    """setup.ps1 under `irm … | iex` runs with no arguments, so on Windows an
    option can only arrive in the environment; the docs offered --no-start and
    --version there, and only SYNAPSE_YES worked (G1 found it)."""
    from synapse.installer import __main__ as entry

    monkeypatch.setenv("SYNAPSE_YES", "1")
    monkeypatch.setenv("SYNAPSE_NO_START", "1")
    monkeypatch.setenv("SYNAPSE_NO_BROWSER", "1")
    monkeypatch.setenv("SYNAPSE_INSTALL_VERSION", "2.0.3")
    monkeypatch.setenv("SYNAPSE_WITH", "knowledge, telemetry")

    assert entry.main([]) == 0
    o = captured_options[0]
    assert (o.yes, o.no_start, o.no_browser, o.version, o.extras) == (
        True, True, True, "2.0.3", ("knowledge", "telemetry"))


def test_a_flag_wins_over_its_twin(captured_options, monkeypatch):
    from synapse.installer import __main__ as entry

    monkeypatch.setenv("SYNAPSE_INSTALL_VERSION", "2.0.3")
    monkeypatch.setenv("SYNAPSE_WITH", "telemetry")
    monkeypatch.setenv("SYNAPSE_NO_START", "0")

    assert entry.main(["upgrade", "--version", "2.0.4", "--with", "knowledge"]) == 0
    o = captured_options[0]
    assert (o.version, o.extras, o.no_start, o.upgrade) == ("2.0.4", ("knowledge",), False, True)


def test_an_unknown_extra_is_refused_before_anything_runs(captured_options, capsys):
    from synapse.installer import __main__ as entry

    with pytest.raises(SystemExit) as exit_info:
        entry.main(["--with", "knowlege"])

    assert exit_info.value.code == 2
    assert "knowledge, telemetry" in capsys.readouterr().err
    assert captured_options == []


# ── the model CLIs found on this computer (ONB2, F727, D303) ─────────────────
# The installer cannot import core/, so it keeps its own copy of CLIM1's
# readers (core/providers/cli_status.py); each is held to that one here.

_CODEX_CATALOG = {"models": [
    {"slug": "gpt-5.6-luna", "visibility": "list", "supported_in_api": True, "priority": 2},
    {"slug": "gpt-6-luna", "visibility": "list", "priority": 1},
    {"slug": "codex-auto-review", "visibility": "hide", "priority": 0},
    {"slug": "gpt-5.6-terra", "visibility": "list", "supported_in_api": False, "priority": 3},
    {"slug": "gpt-5.5", "visibility": "list"},
]}

_COPILOT_CONFIG_HELP = """Configuration Settings:

  `allowed_urls`: URLs the CLI may fetch.
    - "https://example.com"

  `model`: AI model to use for Copilot CLI; can be changed with /model command or --model flag option.
    - "claude-sonnet-5.5"
    - "gpt-5.6"
    - "gemini-3-pro-preview"

  `theme`: the colour theme.
    - "auto"
    - "dark"
"""


def _cli_runs(monkeypatch, answers: dict):
    """`providers._run_cli` answering from `answers` {argv[1:] joined: (code, out)}; anything else fails."""
    ran = []

    def run(*argv, timeout=8.0):
        ran.append(argv)
        return answers.get(" ".join(argv[1:]), (1, ""))

    monkeypatch.setattr(providers, "_run_cli", run)
    return ran


def test_claude_code_is_offered_by_its_aliases_not_fixed_ids():
    """Fixed 4.6 ids went stale; an alias is the newest model of its kind the account can use (CLIM1)."""
    assert providers.CLAUDE_CLI_MODELS == ["cli.claude.sonnet", "cli.claude.opus", "cli.claude.haiku", "cli.claude"]
    from core.providers.cli_status import MODEL_LISTS
    aliases = MODEL_LISTS["cli.claude"][-1][1]
    for m in providers.CLAUDE_CLI_MODELS[:-1]:
        assert m.removeprefix("cli.claude.") in aliases, m
    found = providers.Found(ollama=[], claude=True, gemini=False, codex=False)
    # Claude Code; the everyday model (Sonnet); no other key; Synapse AI's the same.
    answer = providers.ask(_asker(["enter", "enter", "enter", "enter"]), found)
    assert answer == {"model": "cli.claude.sonnet", "mode": "cli"}


def test_gemini_clis_names_are_its_own():
    from core.providers.cli_status import MODEL_LISTS
    for _, names in MODEL_LISTS["cli.gemini"]:
        for m in providers.GEMINI_CLI_MODELS[:-1]:
            assert m.removeprefix("cli.gemini.") in names, m


def test_codex_lists_the_models_it_lets_this_account_pick(monkeypatch):
    from core.providers.cli_status import _codex_models
    _cli_runs(monkeypatch, {"debug models": (0, json.dumps(_CODEX_CATALOG))})
    assert providers.codex_models("/bin/codex") == _codex_models(_CODEX_CATALOG) == ["gpt-6-luna", "gpt-5.6-luna", "gpt-5.5"]
    assert providers.cli_models("codex") == ["cli.codex.gpt-6-luna", "cli.codex.gpt-5.6-luna", "cli.codex.gpt-5.5", "cli.codex"]


def test_codex_falls_back_to_its_cache_file(monkeypatch, tmp_path):
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [{"slug": "gpt-5.4", "visibility": "list"}]}))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    ran = _cli_runs(monkeypatch, {})
    assert providers.codex_models("/bin/codex") == ["gpt-5.4"]
    assert ran and ran[0][1:] == ("debug", "models"), "its own list first; the cache can lag"


def test_codex_with_no_list_is_its_default(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _cli_runs(monkeypatch, {})
    assert providers.cli_models("codex") == ["cli.codex"]


def test_codex_is_asked_only_when_picked(monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", lambda cmd, *a, **k: f"/bin/{cmd}" if cmd == "codex" else None)
    monkeypatch.setattr(providers, "ollama_models", lambda *a, **k: [])
    ran = _cli_runs(monkeypatch, {"debug models": (0, json.dumps(_CODEX_CATALOG))})
    found = providers.detect()
    assert found.codex and not ran, "finding it runs nothing"
    # Codex; its first model; no key; Synapse AI's the same.
    answer = providers.ask(_asker(["enter", "enter", "enter", "enter"]), found)
    assert answer == {"model": "cli.codex.gpt-6-luna", "mode": "cli"}


def test_copilot_cli_is_found_and_offered(monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", lambda cmd, *a, **k: f"/bin/{cmd}" if cmd == "copilot" else None)
    monkeypatch.setattr(providers, "ollama_models", lambda *a, **k: [])
    found = providers.detect()
    assert found.copilot and not (found.claude or found.gemini or found.codex)
    asked = []

    class Spy(widgets.Asker):
        def select(self, title, options, **kw):
            asked.append((title, [o.label for o in options]))
            return super().select(title, options, **kw)

    _cli_runs(monkeypatch, {"help config": (0, _COPILOT_CONFIG_HELP)})
    s = screen.LiveScreen(tui.Term(stream=io.StringIO()), "t")
    answer = providers.ask(Spy(s, FakeKeys(["enter", "enter", "enter", "enter"])), found)
    assert asked[0][1][0] == "GitHub Copilot CLI"
    assert answer == {"model": "cli.copilot.auto", "mode": "cli"}


def test_copilots_models_are_its_settings_choices_auto_first(monkeypatch):
    from core.providers.cli_status import _setting_choices
    _cli_runs(monkeypatch, {"help config": (0, _COPILOT_CONFIG_HELP)})
    assert providers.copilot_models("/bin/copilot") == ["auto", *_setting_choices(_COPILOT_CONFIG_HELP, "model")]
    assert providers.copilot_models("/bin/copilot") == ["auto", "claude-sonnet-5.5", "gpt-5.6", "gemini-3-pro-preview"]
    _cli_runs(monkeypatch, {})
    assert providers.copilot_models("/bin/copilot") == ["auto"]


def test_the_time_left_fits_an_80_column_window():
    """The bar gives up its room to the detail, which would otherwise be clipped (F1037)."""
    class Narrow(tui.Term):
        width = 80

    term = Narrow(stream=io.StringIO())
    live = screen.LiveScreen(term, "Synapse")
    live.add("install", "Installing Synapse")
    live.start("install")
    live.progress("install", 0.66, "312 of 406 MB · about 2 min left")
    line = next(term.clip(l) for l in live.lines() if "Installing" in l)
    assert line.endswith("312 of 406 MB · about 2 min left") and len(line) <= 80
