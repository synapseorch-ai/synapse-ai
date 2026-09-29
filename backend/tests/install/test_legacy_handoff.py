"""
A v1.x script install hands over to 2.0; a 2.x install upgrades the way it was installed.

v1.x's `synapse upgrade` unpacks the newest source over its own folder and re-runs
`synapse/cli.py` with its old venv's Python (v1.9.1 cli.py:1199-1208). 2.0 has to
recognise that position before it imports anything that venv might lack, and
never mistake a checkout or a packaged install for it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

cli = pytest.importorskip("synapse.cli")
from synapse.installer import handoff  # noqa: E402


@pytest.fixture
def old_root(tmp_path, monkeypatch):
    root = tmp_path / "SynapseAI"
    (root / "backend" / "venv").mkdir(parents=True)
    (root / ".installed").write_text("{}")
    monkeypatch.setattr(cli, "ROOT_DIR", root)
    monkeypatch.setattr(cli, "BACKEND_DIR", root / "backend")
    return root


def test_a_v1_script_install_is_recognised(old_root):
    assert cli._legacy_install_root() == old_root


def test_a_checkout_is_never_handed_over(old_root):
    (old_root / ".git").mkdir()
    assert cli._legacy_install_root() is None


def test_without_the_marker_or_the_venv_nothing_is_handed_over(old_root):
    (old_root / ".installed").unlink()
    assert cli._legacy_install_root() is None


def test_this_repo_is_not_a_legacy_install():
    assert cli._legacy_install_root() is None


def test_main_hands_over_before_anything_else(old_root, monkeypatch):
    seen = []
    monkeypatch.setattr(handoff, "run", lambda root, argv: seen.append((root, argv)) or 0)
    monkeypatch.setattr(sys, "argv", ["synapse", "upgrade"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert seen == [(old_root, ["upgrade"])]


def test_the_handoff_forgets_the_old_venv_and_data_folder(old_root, monkeypatch):
    venv_bin = str(old_root / "backend" / "venv" / "bin")
    monkeypatch.setenv("PATH", os.pathsep.join([venv_bin, "/usr/bin"]))
    monkeypatch.setenv("SYNAPSE_UPGRADE_SKIP_DOWNLOAD", "1")
    monkeypatch.setenv("SYNAPSE_DATA_DIR", "backend/data")
    monkeypatch.setenv("VIRTUAL_ENV", str(old_root / "backend" / "venv"))

    handoff._clean_environment(old_root)

    assert os.environ["PATH"] == "/usr/bin"
    for var in ("SYNAPSE_UPGRADE_SKIP_DOWNLOAD", "SYNAPSE_DATA_DIR", "VIRTUAL_ENV"):
        assert var not in os.environ


# ── how a 2.x install upgrades ───────────────────────────────────────────────

def test_the_install_method_comes_from_the_running_process(monkeypatch, tmp_path):
    assert cli._install_method() == "checkout"

    monkeypatch.setattr(cli, "_is_packaged", lambda: True)
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    assert cli._install_method() == "pip"
    (tmp_path / "uv-receipt.toml").write_text("")
    assert cli._install_method() == "uv"
    monkeypatch.setenv("SYNAPSE_LAUNCHER", "npm")
    assert cli._install_method() == "npm"


def test_up_to_date_says_so_and_changes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_install_method", lambda: "uv")
    monkeypatch.setattr(cli, "_get_current_version", lambda: "2.1.0")
    monkeypatch.setattr(cli, "_latest_version", lambda: "2.1.0")
    monkeypatch.setattr(cli, "_upgrade_with_installer", lambda v: pytest.fail("should not upgrade"))
    cli._upgrade_command()
    assert capsys.readouterr().out.strip() == "Synapse 2.1.0 is up to date."


def test_a_newer_release_goes_to_its_own_installer(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_install_method", lambda: "uv")
    monkeypatch.setattr(cli, "_get_current_version", lambda: "2.0.0")
    monkeypatch.setattr(cli, "_latest_version", lambda: "2.0.10")
    monkeypatch.setattr(cli, "_upgrade_with_installer", seen.append)
    cli._upgrade_command()
    assert seen == ["2.0.10"]


def test_npm_is_told_the_npm_command(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_install_method", lambda: "npm")
    monkeypatch.setattr(cli, "_get_current_version", lambda: "2.0.0")
    monkeypatch.setattr(cli, "_latest_version", lambda: "2.1.0")
    cli._upgrade_command()
    assert "npm install -g synapse-orch-ai@2.1.0" in capsys.readouterr().out


def test_a_checkout_is_told_to_use_git(capsys):
    with pytest.raises(SystemExit):
        cli._upgrade_command()
    assert "git pull" in capsys.readouterr().out


def test_no_pypi_is_an_error_not_up_to_date(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_install_method", lambda: "uv")
    monkeypatch.setattr(cli, "_latest_version", lambda: None)
    with pytest.raises(SystemExit) as exit_info:
        cli._upgrade_command()
    assert exit_info.value.code == 1
    assert "Could not reach PyPI" in capsys.readouterr().out


def test_a_folder_the_upgrade_could_not_remove_goes_at_the_next_command(monkeypatch, tmp_path):
    """Windows would not delete the 1.x folder the hand-off was running from."""
    import json

    home = tmp_path / ".synapse"
    home.mkdir()
    old = tmp_path / "SynapseAI"
    (old / "backend").mkdir(parents=True)
    gone = tmp_path / "already-gone"
    (home / "install.json").write_text(json.dumps(
        {"version": "2.0.0", "pending_cleanup": [str(old), str(gone)]}))
    monkeypatch.setenv("SYNAPSE_HOME", str(home))

    cli._finish_pending_cleanup()

    assert not old.exists()
    record = json.loads((home / "install.json").read_text())
    assert "pending_cleanup" not in record and record["version"] == "2.0.0"


def test_uninstall_keeps_the_data_when_asked(monkeypatch, tmp_path, capsys):
    from synapse.installer import autostart

    home = tmp_path / ".synapse"
    for sub in ("var", "backups", "runtime/node", "run", "logs"):
        (home / sub).mkdir(parents=True)
    (home / "install.json").write_text("{}")
    (home / ".env").write_text("K=v")
    monkeypatch.setenv("SYNAPSE_HOME", str(home))
    monkeypatch.setattr(cli, "_install_method", lambda: "pip")
    monkeypatch.setattr(autostart, "disable", lambda: None)
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")

    cli._uninstall_command(keep_data=True)

    assert sorted(p.name for p in home.iterdir()) == [".env", "backups", "var"]
    assert calls[-1][-1] == "synapse-orch-ai"
    assert "Your data is still in" in capsys.readouterr().out


def test_uninstall_without_yes_removes_nothing(monkeypatch, tmp_path):
    home = tmp_path / ".synapse"
    (home / "var").mkdir(parents=True)
    monkeypatch.setenv("SYNAPSE_HOME", str(home))
    monkeypatch.setattr(cli, "_install_method", lambda: "uv")
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    cli._uninstall_command()
    assert (home / "var").is_dir()
