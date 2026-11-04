"""
The installer from start to finish, with a fake uv and a fake installed CLI.

What is being tested is the order of things and what the user is shown: that a
v1.x install is stopped and backed up before anything touches it, that its data
is imported before 2.0 first starts, that the old folder goes only at the end,
and that the screen ends with the link — or, when something fails, says which
step, why, and what is still safe.

POSIX only: the fakes are executable Python scripts run through their shebang.
"""
from __future__ import annotations

import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="fake executables need a shebang")

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from synapse.installer import flow, legacy, runtime, tui  # noqa: E402

#: The Browser step as shipped: the `world` replaces it with a stub for every other test.
_REAL_BROWSER = flow.Installer._browser

FAKE_CLI = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"] + ".argv", "a") as f:
    f.write(json.dumps(args) + "\n")
if args[:3] == ["-P", "-m", "synapse"]:
    args = args[3:]
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(json.dumps(args) + "\n")
if args == ["--version"]:
    print("synapse 2.0.0")
elif args[:2] == ["setup", "--apply"]:
    data = json.loads(sys.stdin.read())
    with open(os.environ["FAKE_CALLS"] + ".apply", "w") as f:
        json.dump(data, f)
    print(json.dumps({"saved": sorted(data["settings"]), "ignored": [], "default_agent": "created"}))
elif args[:1] == ["migrate"]:
    sources = [args[i + 1] for i, a in enumerate(args) if a == "--from"]
    for s in sources:
        os.rename(s, s + ".migrated")
    print("[store] noise on stderr", file=sys.stderr)
    print(json.dumps({"database": "ok",
                      "sources": [{"path": s, "counts": {"user_agents": 2}, "files": {},
                                   "retired_to": s + ".migrated"} for s in sources],
                      "logs": [], "totals": {"user_agents": 2 * len(sources), "chat_sessions": 1}}))
elif args[:1] == ["start"]:
    print("Synapse is running -> http://localhost:3000/?token=tok")
elif args[:2] == ["status", "--json"]:
    print(json.dumps({"ready": True, "link": "http://localhost:3000/?token=tok",
                      "backend": {"running": True}}))
elif args[:1] == ["stop"]:
    pass
elif args == ["setup", "--knowledge-models"]:
    if os.environ.get("FAKE_MODELS_FAIL"):
        print(json.dumps({"models": "failed", "error": "huggingface.co did not answer"}))
        sys.exit(1)
    print(json.dumps({"models": os.environ.get("FAKE_MODELS", "fetched"), "mb": 669,
                      "path": "/x/models/docling"}))
else:
    sys.exit("unexpected: %s" % args)
'''

FAKE_UV = r'''
import json, os, shutil, sys
args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(json.dumps(["uv"] + args) + "\n")
tools, bins = os.environ["FAKE_TOOLDIR"], os.environ["FAKE_BINDIR"]
if args[:1] == ["-c"]:
    print("3.12.8")                  # run as the Python `uv python find` named
elif args[:2] == ["python", "install"]:
    print("Installed Python 3.12.8 in 1.02s")
elif args[:2] == ["python", "find"]:
    print(sys.argv[0])
elif args[:2] == ["tool", "install"] and "--help" in args:
    # An older uv takes no --torch-backend on `tool install`.
    print("Usage: uv tool install [OPTIONS] <PACKAGE>")
    if not os.environ.get("FAKE_UV_OLD"):
        print("      --torch-backend <TORCH_BACKEND>  The backend to use when fetching packages in the PyTorch ecosystem")
elif args[:2] == ["tool", "install"]:
    if "-c" in args:
        # As uv 0.11 does: the value is split at whitespace, each part a file
        # read from the working directory (F834).
        for part in args[args.index("-c") + 1].split():
            if not os.path.isfile(part):
                print("error: File not found: `%s`" % part)
                sys.exit(2)
    if os.environ.get("FAKE_UV_FAIL"):
        print("Resolved 181 packages in 1.2s")
        print("error: Failed to download `torch==2.9.0`: connection reset")
        sys.exit(2)
    for line in ["Resolved 3 packages in 0.1s", "Downloading big (20.0MiB)", " Downloaded big",
                 "Prepared 3 packages in 0.2s", "Installed 3 packages in 1ms",
                 "Installed 2 executables: synapse, synapse-ai"]:
        print(line)
    venv = os.path.join(tools, "synapse-orch-ai", "bin")
    os.makedirs(venv, exist_ok=True)
    shutil.copy(os.environ["FAKE_CLI_SRC"], os.path.join(venv, "python"))
    os.chmod(os.path.join(venv, "python"), 0o755)
    os.makedirs(bins, exist_ok=True)
elif args == ["tool", "dir"]:
    print(tools)
elif args == ["tool", "dir", "--bin"]:
    print(bins)
elif args == ["tool", "update-shell"]:
    print("Updated .bashrc")
else:
    sys.exit("unexpected uv: %s" % args)
'''


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A home directory, a fake uv and CLI, and no real Node or browser download."""
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("FAKE_CALLS", str(calls))
    monkeypatch.setenv("FAKE_TOOLDIR", str(tmp_path / "uv-tools"))
    monkeypatch.setenv("FAKE_BINDIR", str(tmp_path / "uv-bin"))
    monkeypatch.setenv("FAKE_CLI_SRC", str(_script(fakes / "cli", FAKE_CLI)))
    monkeypatch.delenv("SYNAPSE_PACKAGE", raising=False)
    monkeypatch.delenv("SYNAPSE_HOME", raising=False)
    monkeypatch.setattr(runtime, "find_node",
                        lambda home: (Path("/usr/bin/node"), (22, 11, 0), "system"))
    monkeypatch.setattr(flow.Installer, "_browser", lambda self: "Chromium")
    monkeypatch.setattr(flow, "_open", lambda url: None)
    monkeypatch.setattr(flow.autostart, "supported", lambda: False)
    # This machine may be running a backend on 8765; the flow only needs a free one.
    monkeypatch.setattr(flow, "BACKEND_PORT", flow.ports.free_port(28765))
    monkeypatch.setattr(legacy.shutil, "which", lambda *a, **k: None)
    uv = _script(fakes / "uv", FAKE_UV)

    def install(version_override=None, home=None, **opts):
        out = io.StringIO()
        version = "2.0.0" if version_override is None else version_override
        options = flow.Options(uv=str(uv), yes=True, version=version,
                               home=home or tmp_path / ".synapse", **opts)
        code = flow.Installer(options, term=tui.Term(stream=out)).run()
        recorded = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        return code, out.getvalue(), recorded

    return {"tmp": tmp_path, "home": tmp_path / ".synapse", "install": install, "calls": calls}


def _names(recorded):
    return [" ".join(c[:2]) for c in recorded]


def test_a_fresh_install_ends_with_the_link(world):
    code, out, calls = world["install"]()

    assert code == 0, out
    assert _names(calls) == [
        "uv python", "uv python", "uv -c", "uv tool", "uv tool", "uv tool", "--version",
        "setup --apply", "start --detach", "status --json", "uv tool",
    ]
    install_call = calls[3]
    assert install_call[:4] == ["uv", "tool", "install", "--force"]
    assert install_call[-1] == "synapse-orch-ai==2.0.0"
    applied = json.loads(Path(str(world["calls"]) + ".apply").read_text())
    assert applied["default_agent"] is True
    assert applied["settings"]["backend_port"] and applied["settings"]["frontend_port"]
    assert applied["settings"]["start_on_boot"] is False
    assert "installed_at" in applied["settings"]
    # Every step on its own line, then the link — and nothing any command printed.
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[0] == "Synapse 2.0.0"
    assert lines[1].startswith("[1/6] Getting Python") and lines[1].endswith("ok  3.12.8")
    assert "Resolved" not in out and "Downloaded" not in out
    assert "Synapse is running -> http://localhost:3000/?token=tok" in out.replace("→", "->")
    assert "Open a new terminal to use the synapse command." in out
    record = json.loads((world["home"] / "install.json").read_text())
    assert record["version"] == "2.0.0" and record["method"] == "uv"
    assert record["tool_python"].endswith("synapse-orch-ai/bin/python")
    log = (world["home"] / "logs" / "install.log").read_text()
    assert "Downloaded big" in log


def _dry_run_names(monkeypatch, *folders):
    """Playwright's own `install --dry-run --only-shell chromium`, printing the folders it wants as it does (F1362)."""
    real = flow.subprocess.run

    def fake(cmd, *a, **kw):
        if "--dry-run" in [str(c) for c in cmd]:
            out = "".join(f"browser: part\n  Install location:    {f}\n  Download url:        https://cdn.example/x.zip\n\n"
                          for f in folders)
            return flow.subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(flow.subprocess, "run", fake)


def test_the_browser_step_fetches_the_headless_shell_only(world, monkeypatch):
    """F1355: the browser component and the web reader launch headless Chromium, which runs on the headless shell;
    the full build (184 MiB, about 30 s of every install and upgrade) was WhatsApp Web's alone, and that is gone."""
    ran = []
    monkeypatch.setattr(flow, "run", lambda cmd, log, **kw: ran.append([str(a) for a in cmd]))
    browsers = world["tmp"] / "ms-playwright"
    monkeypatch.setattr(flow, "_playwright_dir", lambda: browsers)
    installer = flow.Installer(flow.Options(uv="uv", yes=True, home=world["home"]), term=tui.Term(stream=io.StringIO()))
    installer.s.tool_python = Path("/tool/python")

    assert _REAL_BROWSER(installer) == "Chromium"      # the world stubs _browser; this is the real one
    assert ran == [["/tool/python", "-m", "playwright", "install", "--only-shell", "chromium"]]

    ran.clear()
    _dry_run_names(monkeypatch, browsers / "chromium_headless_shell-1234", browsers / "ffmpeg-1011")
    (browsers / "chromium_headless_shell-1234").mkdir(parents=True)
    (browsers / "ffmpeg-1011").mkdir()
    _REAL_BROWSER(installer)
    assert ran == [], "the headless shell was there, and it was fetched again"


def test_an_older_headless_shell_from_1x_does_not_make_the_browser_step_skip(world, monkeypatch):
    """F1362 (REL3's rc4 upgrade of a real 1.9.3): 1.9's own start had left older shells (1234, 1252); the step saw a
    `chromium_headless_shell` folder and fetched nothing, so 2.0's Playwright (1.64, which wants 1248) downloaded its
    shell silently at Starting, with no bar. The step asks Playwright which build it wants, as `synapse start` does."""
    ran = []
    monkeypatch.setattr(flow, "run", lambda cmd, log, **kw: ran.append([str(a) for a in cmd]))
    browsers = world["tmp"] / "ms-playwright"
    monkeypatch.setattr(flow, "_playwright_dir", lambda: browsers)
    for old in ("chromium-1234", "chromium_headless_shell-1234", "chromium_headless_shell-1252", "ffmpeg-1011"):
        (browsers / old).mkdir(parents=True)
    _dry_run_names(monkeypatch, browsers / "chromium_headless_shell-1248", browsers / "ffmpeg-1011")
    installer = flow.Installer(flow.Options(uv="uv", yes=True, home=world["home"]), term=tui.Term(stream=io.StringIO()))
    installer.s.tool_python = Path("/tool/python")

    _REAL_BROWSER(installer)

    assert ran == [["/tool/python", "-m", "playwright", "install", "--only-shell", "chromium"]], \
        "only older shells were there, and the one this Playwright runs was not fetched"


def test_the_browser_step_s_bar_is_full_when_the_shell_is(world, monkeypatch):
    """F1355: what `--only-shell chromium` prints is the headless shell's bar and then FFmpeg's (2.3 MiB): the step's
    bar reads full when the shell is down, never half, and FFmpeg does not move it."""
    lines = [f"|{'■' * (p * 80 // 100):<80}| {p:>3}% of 114.7 MiB" for p in (0, 40, 100)]
    lines += [f"|{'■' * (p * 80 // 100):<80}| {p:>3}% of 2.3 MiB" for p in (0, 100)]
    shown = []

    def run(cmd, log, on_line=None, **kw):
        for line in lines:
            on_line(line)

    class Screen:
        def progress(self, key, fraction):
            shown.append((key, fraction))

    monkeypatch.setattr(flow, "run", run)
    monkeypatch.setattr(flow, "_playwright_dir", lambda: world["tmp"] / "ms-playwright")
    installer = flow.Installer(flow.Options(uv="uv", yes=True, home=world["home"]), term=tui.Term(stream=io.StringIO()))
    installer.s.tool_python = Path("/tool/python")
    installer.screen = Screen()

    _REAL_BROWSER(installer)

    assert [f for _, f in shown] == [0.0, 0.4, 1.0, 1.0, 1.0]


def test_the_installed_cli_runs_with_the_working_directory_off_sys_path(world, monkeypatch):
    """The 1.x hand-off runs from inside the old folder, whose synapse/ is 2.0's
    source: `python -m synapse` without -P imported that, not the new install,
    and it started a second installer (found by scripts/e2e/legacy-upgrade.sh)."""
    decoy = world["tmp"] / "old-install"
    (decoy / "synapse").mkdir(parents=True)
    monkeypatch.chdir(decoy)

    code, out, calls = world["install"]()

    assert code == 0, out
    argv = [json.loads(line) for line in Path(str(world["calls"]) + ".argv").read_text().splitlines()]
    assert argv and all(a[:3] == ["-P", "-m", "synapse"] for a in argv), argv


def test_a_taken_backend_port_stops_before_anything_is_installed(world, monkeypatch):
    """The backend stays on one port (D186), so a taken one stops the install
    with a reason instead."""
    import socket

    with socket.socket() as taken:
        taken.bind(("0.0.0.0", 0))
        taken.listen()
        monkeypatch.setattr(flow, "BACKEND_PORT", taken.getsockname()[1])

        code, out, calls = world["install"]()

    assert code == 1
    assert "is in use by another program, and Synapse's backend needs it" in out
    assert not [c for c in calls if c[:3] == ["uv", "tool", "install"]]


def test_no_start_installs_and_says_how_to_start(world):
    code, out, calls = world["install"](no_start=True)
    assert code == 0
    assert "start --detach" not in _names(calls)
    assert "Synapse 2.0.0 is installed. Start it with: synapse start" in out


def test_a_failed_download_says_which_step_why_and_how_to_retry(world, monkeypatch):
    monkeypatch.setenv("FAKE_UV_FAIL", "1")

    code, out, calls = world["install"]()

    assert code == 1
    assert "[2/6] Installing Synapse" in out and "FAILED" in out
    assert "Installing Synapse failed: uv could not install Synapse" in out
    assert "connection reset" in out            # the tool's own last words
    assert "Full log: ~/.synapse/logs/install.log" in out
    assert "Try again: curl -LsSf" in out
    assert "setup --apply" not in _names(calls)


def test_a_v19_install_is_stopped_backed_up_moved_then_removed(world):
    tmp = world["tmp"]
    root = tmp / ".local" / "share" / "SynapseAI"
    data = root / "backend" / "data"
    (data / "chat_sessions").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "synapse").mkdir()
    (root / ".installed").write_text("{}")
    (root / "pyproject.toml").write_text('version = "1.9.0"\n')
    (root / ".env").write_text("SYNAPSE_DATA_DIR=backend/data\nSYNAPSE_JWT_SECRET=keep-me\n")
    (data / "user_agents.json").write_text(json.dumps([{"id": "a1"}, {"id": "a2"}]))
    (data / "settings.json").write_text(json.dumps({"model": "claude-sonnet-4-6",
                                                    "frontend_port": 3000}))
    (data / "chat_sessions" / "a1_s.json").write_text("{}")
    (tmp / ".bashrc").write_text(f'# Synapse AI\nexport PATH="{root / "bin"}:$PATH"\n')

    code, out, calls = world["install"]()

    assert code == 0, out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[0] == "Synapse 1.9.0 -> 2.0.0" or lines[0] == "Synapse 1.9.0 → 2.0.0"
    assert "[1/10] Stopping Synapse 1.9.0" in lines[1]
    assert "[2/10] Backing up" in lines[2]
    # Imported before 2.0 first starts, from the folder the install ran with.
    names = _names(calls)
    assert names.index("migrate --json") < names.index("start --detach")
    migrate = next(c for c in calls if c[:1] == ["migrate"])
    assert migrate[migrate.index("--from") + 1] == str(data)
    # The old model came across, so nothing was asked and nothing overwrote it —
    # and the screen names it rather than saying to choose one later.
    applied = json.loads(Path(str(world["calls"]) + ".apply").read_text())
    assert "model" not in applied["settings"]
    setting_up = next(line for line in lines if "Setting up" in line)
    assert setting_up.endswith("claude-sonnet-4-6, from 1.9.0"), setting_up
    # The secrets came across; the old path did not.
    env = (world["home"] / ".env").read_text()
    assert "SYNAPSE_JWT_SECRET=keep-me" in env and "SYNAPSE_DATA_DIR" not in env
    # The backup exists and holds the data; the old install and its PATH line are gone.
    backups = list((world["home"] / "backups").glob("synapse-1.9.0-*.tar.gz"))
    assert len(backups) == 1
    assert not root.exists()
    assert str(root) not in (tmp / ".bashrc").read_text()
    record = json.loads((world["home"] / "install.json").read_text())
    assert record["migrated_from"]["version"] == "1.9.0"
    assert record["migrated_from"]["backup"] == str(backups[0])
    # Nothing was running and no setting needs a word: the end is the link alone (F1350, F1353).
    assert "your old address" not in out and "SYNAPSE_TRANSFORM_RUNTIME" not in out


def _v19(tmp: Path, settings: dict) -> Path:
    """A 1.9 script install as setup.sh left it, its settings.json the 1.x model plus `settings`."""
    root = tmp / ".local" / "share" / "SynapseAI"
    data = root / "backend" / "data"
    (data / "chat_sessions").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "synapse").mkdir()
    (root / ".installed").write_text("{}")
    (root / "pyproject.toml").write_text('version = "1.9.0"\n')
    (root / ".env").write_text("SYNAPSE_DATA_DIR=backend/data\n")
    (data / "user_agents.json").write_text(json.dumps([{"id": "a1"}]))
    (data / "settings.json").write_text(json.dumps({"model": "claude-sonnet-4-6", **settings}))
    return root


def _listen(cwd: Path, script: str):
    """An interpreter running `script` under `cwd` and listening on a free port, in its own session, reaped
    when it ends. Returns the process and its port."""
    import subprocess
    import threading
    import time

    port_file = cwd / ".port"
    (cwd / Path(script).parent).mkdir(parents=True, exist_ok=True)
    (cwd / script).write_text(
        "import socket, time\n"
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
def test_a_v19_web_page_left_running_is_stopped_and_its_port_kept(world):
    """F1350, as REL3 met it upgrading a real 1.9.3: its own `synapse upgrade` stopped the backend and removed the
    PID files, but the web page kept the old port. The hand-off stops it, says so, and 2.0 takes the old port."""
    root = _v19(world["tmp"], {})
    proc, port = _listen(root / "frontend", "scripts/load-env.py")
    (root / "backend" / "data" / "settings.json").write_text(
        json.dumps({"model": "claude-sonnet-4-6", "frontend_port": port}))
    try:
        code, out, _ = world["install"]()

        assert code == 0, out
        stopping = next(line for line in out.splitlines() if "Stopping Synapse" in line)
        assert f"stopped the old web page ({os.path.basename(sys.executable)} … load-env.py start, " \
               f"pid {proc.pid})" in stopping, stopping
        assert proc.wait(timeout=10) is not None
        applied = json.loads(Path(str(world["calls"]) + ".apply").read_text())
        assert applied["settings"]["frontend_port"] == port
        assert "your old address" not in out
    finally:
        proc.kill()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_an_old_address_another_program_holds_is_said(world):
    """F1350: what holds the old port runs no code of the old install's, so it is left alone, and the move to
    another port is said rather than made quietly."""
    root = _v19(world["tmp"], {})
    proc, port = _listen(world["tmp"] / "someone-else", "serve.py")
    (root / "backend" / "data" / "settings.json").write_text(
        json.dumps({"model": "claude-sonnet-4-6", "frontend_port": port}))
    try:
        code, out, _ = world["install"]()

        assert code == 0, out
        assert proc.poll() is None
        applied = json.loads(Path(str(world["calls"]) + ".apply").read_text())
        assert applied["settings"]["frontend_port"] != port
        assert (f"Port {port}, your old address, is in use by another program, so Synapse is now on port "
                f"{applied['settings']['frontend_port']}.") in out
    finally:
        proc.kill()


def test_the_kept_model_is_named_when_the_browser_step_ran(world, monkeypatch):
    """F1352: the Browser step keeps its folder in the answers, so they are not empty once it ran; the 1.x model
    came across all the same, and the screen names it rather than saying to choose one later."""
    _v19(world["tmp"], {})
    monkeypatch.setattr(flow.Installer, "_browser",
                        lambda self: self.s.answers.setdefault("playwright_browsers_path", "/b") and "Chromium")

    code, out, _ = world["install"]()

    assert code == 0, out
    setting_up = next(line for line in out.splitlines() if "Setting up" in line)
    assert setting_up.endswith("claude-sonnet-4-6, from 1.9.0"), setting_up
    assert "choose an AI later" not in out


def test_a_host_run_transform_setting_is_said_at_the_end(world):
    """F1353: 1.x ran Transform steps on this computer from a setting 2.0 no longer reads (F968); the end of the
    upgrade says how to keep that, before the first Transform meets Docker."""
    _v19(world["tmp"], {"transform_runtime": "host"})

    code, out, _ = world["install"]()

    assert code == 0, out
    end = out[out.index("is running"):]
    assert ("2.0 runs code steps in Docker. To keep running them on this computer, set "
            "SYNAPSE_TRANSFORM_RUNTIME=host (see Upgrading to 2.0).") in end
    assert "2.0 runs code steps in Docker" in (world["home"] / "logs" / "install.log").read_text()


def test_a_failure_ends_the_open_question(world, monkeypatch):
    """Nobody should be left answering questions for an install that has failed."""
    monkeypatch.setenv("FAKE_UV_FAIL", "1")

    class Silent(tui.Keys):
        def ready(self, timeout):
            import time
            time.sleep(timeout)
            return False

    term = tui.Term(stream=io.StringIO())
    term.keys = lambda: Silent()
    fakes = world["tmp"] / "fakes"
    options = flow.Options(uv=str(fakes / "uv"), version="2.0.0", home=world["home"])

    code = flow.Installer(options, term=term).run()

    assert code == 1
    assert "Installing Synapse failed" in term.stream.getvalue()


def test_not_now_changes_nothing(world, monkeypatch):
    tmp = world["tmp"]
    root = tmp / ".local" / "share" / "SynapseAI"
    (root / "backend" / "data").mkdir(parents=True)
    (root / ".installed").write_text("{}")
    (root / "backend" / "data" / "user_agents.json").write_text("[]")

    class Keys(tui.Keys):
        def __init__(self):
            self.keys = ["down", "enter"]

        def read(self):
            return self.keys.pop(0)

    term = tui.Term(stream=io.StringIO())
    term.keys = lambda: Keys()
    options = flow.Options(uv="/nonexistent", version="2.0.0", home=world["home"])

    code = flow.Installer(options, term=term).run()

    assert code == 0
    assert root.exists() and not (world["home"] / "backups").exists()
    assert "Nothing was changed." in term.stream.getvalue()


# ── extras: kept on upgrade, added with --with, PyTorch from its CPU index (IU1) ─

def _existing(world, extras=None, version="2.0.0"):
    """A 2.x install made by the installer: install.json, its tool venv and uv's receipt."""
    venv = world["tmp"] / "uv-tools" / "synapse-orch-ai"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.write_text(Path(os.environ["FAKE_CLI_SRC"]).read_text())
    python.chmod(0o755)
    requirement = '{ name = "synapse-orch-ai"' + (
        f", extras = {json.dumps(extras)}" if extras else "") + f', specifier = "=={version}" }}'
    (venv / "uv-receipt.toml").write_text(
        f"[tool]\nrequirements = [{requirement}]\npython = \"3.12\"\n", encoding="utf-8")
    world["home"].mkdir(parents=True, exist_ok=True)
    (world["home"] / "install.json").write_text(json.dumps(
        {"method": "uv", "version": version, "tool_python": str(python)}))


def _bundled_constraints(monkeypatch):
    """The two pinned sets a release's pyz carries (a checkout has neither)."""
    monkeypatch.setattr(flow, "_bundled", lambda name: {
        "constraints.txt": "typer==0.27.1\n",
        "constraints-knowledge.txt": "typer==0.26.8\ntorch==2.14.1+cpu ; sys_platform != 'darwin'\n",
    }.get(name, ""))


def _tool_install(calls):
    return next(c for c in calls if c[:3] == ["uv", "tool", "install"] and "--help" not in c)


def test_an_upgrade_keeps_the_extras_uv_installed(world, monkeypatch):
    """`uv tool install --force` replaces the receipt, so an upgrade that did not
    read it first reinstalled without [knowledge] (DOC2's finding; IU1 measured
    torch and docling gone after 2.0.0 → 2.0.1)."""
    _existing(world, extras=["knowledge", "telemetry"])
    _bundled_constraints(monkeypatch)

    code, out, calls = world["install"](upgrade=True, version_override="2.0.1")

    assert code == 0, out
    install = _tool_install(calls)
    assert install[-1] == "synapse-orch-ai[knowledge,telemetry]==2.0.1"
    # Knowledge runs no model on a GPU: without this, Linux gets CUDA's gigabytes.
    assert install[install.index("--torch-backend") + 1] == "cpu"
    # The base set pins typer above docling-core's cap; [knowledge] gets its own.
    assert install[install.index("-c") + 1] == "constraints-knowledge.txt"
    assert "with knowledge, telemetry" in out


def test_an_upgrade_with_no_extras_runs_the_same_command_as_before(world, monkeypatch):
    _existing(world)
    _bundled_constraints(monkeypatch)

    code, out, calls = world["install"](upgrade=True, version_override="2.0.1")

    assert code == 0, out
    install = _tool_install(calls)
    assert install[-1] == "synapse-orch-ai==2.0.1"
    assert "--torch-backend" not in install
    assert install[install.index("-c") + 1] == "constraints.txt"
    assert not [c for c in calls if "--help" in c]      # uv is not asked about PyTorch


def test_with_adds_an_extra_and_a_later_upgrade_keeps_it(world, monkeypatch):
    _existing(world)
    _bundled_constraints(monkeypatch)

    code, out, calls = world["install"](upgrade=True, version_override="2.0.0", extras=("knowledge",))

    assert code == 0, out
    install = _tool_install(calls)
    assert install[-1] == "synapse-orch-ai[knowledge]==2.0.0"
    assert install[install.index("--torch-backend") + 1] == "cpu"


def test_with_telemetry_alone_needs_no_pytorch_flag(world, monkeypatch):
    _bundled_constraints(monkeypatch)

    code, out, calls = world["install"](extras=("telemetry",))

    assert code == 0, out
    install = _tool_install(calls)
    assert install[-1] == "synapse-orch-ai[telemetry]==2.0.0"
    assert "--torch-backend" not in install
    assert install[install.index("-c") + 1] == "constraints.txt"


def test_a_uv_that_cannot_choose_pytorchs_build_is_swapped_for_the_installers(world, monkeypatch):
    """An older system uv rejects --torch-backend; UV_TORCH_BACKEND it would
    silently ignore, and Linux would get CUDA. The pinned private uv takes it."""
    _existing(world, extras=["knowledge"])
    monkeypatch.setenv("FAKE_UV_OLD", "1")
    private = world["tmp"] / "fakes" / "private-uv"
    private.write_text((world["tmp"] / "fakes" / "uv").read_text())
    private.chmod(0o755)
    fetched = []
    monkeypatch.setattr(runtime, "install_uv", lambda home, progress=None: fetched.append(home) or private)

    code, out, calls = world["install"](upgrade=True, version_override="2.0.1")

    assert code == 0, out
    assert fetched == [world["home"]]
    record = json.loads((world["home"] / "install.json").read_text())
    assert record["uv"] == str(private)


def test_a_home_with_a_space_in_its_path_installs(world, monkeypatch):
    """uv splits a constraints path at spaces: Naveen's `C:\\Users\\Naveen Raj`
    home failed with "File not found: C:\\Users\\Naveen" (F834, rc0 on Windows)."""
    _bundled_constraints(monkeypatch)
    home = world["tmp"] / "Jane Doe" / ".synapse"

    code, out, calls = world["install"](home=home)

    assert code == 0, out
    assert _tool_install(calls)[-1] == "synapse-orch-ai==2.0.0"
    assert (home / "runtime" / "constraints.txt").read_text() == "typer==0.27.1\n"


def test_a_relative_wheel_path_and_find_links_still_reach_uv(world, monkeypatch, tmp_path):
    """uv runs from the runtime folder now, so relative paths are made whole first."""
    _bundled_constraints(monkeypatch)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "synapse_orch_ai-2.0.1-py3-none-any.whl").write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SYNAPSE_PACKAGE", "dist/synapse_orch_ai-2.0.1-py3-none-any.whl")
    monkeypatch.setenv("UV_FIND_LINKS", "dist https://example.test/wheels/")
    seen = {}
    real_run = flow.run

    def spy(cmd, log, **kwargs):
        if cmd[1:3] == ["tool", "install"] and "--help" not in cmd:
            seen.update(cmd=cmd, cwd=kwargs.get("cwd"), links=kwargs["env"]["UV_FIND_LINKS"])
        return real_run(cmd, log, **kwargs)

    monkeypatch.setattr(flow, "run", spy)
    code, out, calls = world["install"](version_override="")

    assert code == 0, out
    assert seen["cmd"][-1] == str(tmp_path / "dist" / "synapse_orch_ai-2.0.1-py3-none-any.whl")
    assert seen["cwd"] == world["home"] / "runtime"
    assert seen["links"] == f"{tmp_path / 'dist'} https://example.test/wheels/"


def test_uv_downloads_a_few_at_a_time_and_waits_on_a_slow_link(world, monkeypatch):
    """~50 parallel downloads on Naveen's link left numpy 30 s without a byte, and
    uv gave up on it: "operation timed out" (F835, rc0 on Windows)."""
    monkeypatch.delenv("UV_CONCURRENT_DOWNLOADS", raising=False)
    monkeypatch.delenv("UV_HTTP_TIMEOUT", raising=False)
    seen = {}
    real_run = flow.run

    def spy(cmd, log, **kwargs):
        if cmd[1:3] == ["tool", "install"] and "--help" not in cmd:
            seen.update(kwargs["env"])
        return real_run(cmd, log, **kwargs)

    monkeypatch.setattr(flow, "run", spy)
    code, out, _ = world["install"]()

    assert code == 0, out
    assert seen["UV_CONCURRENT_DOWNLOADS"] == "4"
    assert seen["UV_HTTP_TIMEOUT"] == "300"


def test_a_persons_own_uv_download_settings_are_kept(world, monkeypatch):
    monkeypatch.setenv("UV_CONCURRENT_DOWNLOADS", "16")
    monkeypatch.setenv("UV_HTTP_TIMEOUT", "45")
    seen = {}
    real_run = flow.run

    def spy(cmd, log, **kwargs):
        if cmd[1:3] == ["tool", "install"] and "--help" not in cmd:
            seen.update(kwargs["env"])
        return real_run(cmd, log, **kwargs)

    monkeypatch.setattr(flow, "run", spy)
    code, out, _ = world["install"]()

    assert code == 0, out
    assert (seen["UV_CONCURRENT_DOWNLOADS"], seen["UV_HTTP_TIMEOUT"]) == ("16", "45")


def test_a_wheel_path_takes_its_extras_as_a_direct_reference(world, monkeypatch, tmp_path):
    """SYNAPSE_PACKAGE (the e2e scripts' wheel) cannot take `[extra]` after a path."""
    wheel = tmp_path / "synapse_orch_ai-2.0.1-py3-none-any.whl"
    wheel.write_bytes(b"")
    monkeypatch.setenv("SYNAPSE_PACKAGE", str(wheel))
    _existing(world, extras=["knowledge"])

    code, out, calls = world["install"](upgrade=True, version_override="")

    assert code == 0, out
    assert _tool_install(calls)[-1] == f"synapse-orch-ai[knowledge] @ {wheel.resolve().as_uri()}"


def test_knowledges_models_are_fetched_after_the_install(world, monkeypatch):
    _existing(world, extras=["knowledge"])

    code, out, calls = world["install"](upgrade=True, version_override="2.0.1")

    assert code == 0, out
    names = _names(calls)
    assert names.index("setup --knowledge-models") > names.index("uv tool")
    assert names.index("setup --knowledge-models") < names.index("start --detach")
    models = [line for line in out.splitlines() if "Knowledge's PDF models" in line]
    assert models and models[0].endswith("669 MB"), models


def test_models_already_there_are_not_fetched_again(world, monkeypatch):
    _existing(world, extras=["knowledge"])
    monkeypatch.setenv("FAKE_MODELS", "present")

    code, out, calls = world["install"](upgrade=True, version_override="2.0.1")

    assert code == 0, out
    models = [line for line in out.splitlines() if "Knowledge's PDF models" in line]
    assert models and models[0].endswith("already here"), models
    assert len([c for c in calls if c == ["setup", "--knowledge-models"]]) == 1


def test_a_failed_model_download_does_not_fail_the_install(world, monkeypatch):
    """Synapse starts without them: the basic PDF reader carries on."""
    _existing(world, extras=["knowledge"])
    monkeypatch.setenv("FAKE_MODELS_FAIL", "1")

    code, out, calls = world["install"](upgrade=True, version_override="2.0.1")

    assert code == 0, out
    assert "synapse upgrade --with knowledge" in out
    assert "start --detach" in _names(calls)


def test_no_knowledge_no_models_step(world):
    _existing(world, extras=["telemetry"])

    code, out, calls = world["install"](upgrade=True, version_override="2.0.1")

    assert code == 0, out
    assert "setup --knowledge-models" not in _names(calls)
