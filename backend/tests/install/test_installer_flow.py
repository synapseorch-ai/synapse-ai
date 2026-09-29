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

FAKE_CLI = r'''
import json, os, sys
args = sys.argv[1:]
if args[:2] == ["-m", "synapse"]:
    args = args[2:]
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
else:
    sys.exit("unexpected: %s" % args)
'''

FAKE_UV = r'''
import json, os, shutil, sys
args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(json.dumps(["uv"] + args) + "\n")
tools, bins = os.environ["FAKE_TOOLDIR"], os.environ["FAKE_BINDIR"]
if args[:2] == ["python", "install"]:
    print("Installed Python 3.12.8 in 1.02s")
elif args[:2] == ["tool", "install"]:
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
    monkeypatch.setattr(legacy.shutil, "which", lambda *a, **k: None)
    uv = _script(fakes / "uv", FAKE_UV)

    def install(**opts):
        out = io.StringIO()
        options = flow.Options(uv=str(uv), yes=True, version="2.0.0",
                               home=tmp_path / ".synapse", **opts)
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
        "uv python", "uv tool", "uv tool", "uv tool", "--version",
        "setup --apply", "start --detach", "status --json", "uv tool",
    ]
    install_call = calls[1]
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
    # The old model came across, so nothing was asked and nothing overwrote it.
    applied = json.loads(Path(str(world["calls"]) + ".apply").read_text())
    assert "model" not in applied["settings"]
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
