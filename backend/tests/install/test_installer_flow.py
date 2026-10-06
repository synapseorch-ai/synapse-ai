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

    def install(version_override=None, **opts):
        out = io.StringIO()
        version = "2.0.0" if version_override is None else version_override
        options = flow.Options(uv=str(uv), yes=True, version=version,
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
    assert install[install.index("-c") + 1].endswith("constraints-knowledge.txt")
    assert "with knowledge, telemetry" in out


def test_an_upgrade_with_no_extras_runs_the_same_command_as_before(world, monkeypatch):
    _existing(world)
    _bundled_constraints(monkeypatch)

    code, out, calls = world["install"](upgrade=True, version_override="2.0.1")

    assert code == 0, out
    install = _tool_install(calls)
    assert install[-1] == "synapse-orch-ai==2.0.1"
    assert "--torch-backend" not in install
    assert install[install.index("-c") + 1].endswith(os.sep + "constraints.txt")
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
    assert install[install.index("-c") + 1].endswith(os.sep + "constraints.txt")


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
