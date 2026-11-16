"""A failed pip upgrade that stopped a running Synapse says it is still stopped, and how to start it (F1640).

Every upgrade path stops a running Synapse and backs up the database before it installs. When the install then failed,
the stopped Synapse stayed stopped and the person read only the installer's error. A restart cannot be certain to match
how they ran it (in a terminal, in the background or by the login service is not recorded), so nothing is started on a
guess: the failure says plainly that Synapse was stopped for the upgrade and is still stopped, and how to start it
again. That the data is as it was is said only when no migration ran, which is certain when the install itself failed.
The installer's own paths (uv, and so an npm install's first run) are held in test_installer_flow.

Near misses: a Synapse that was not running before is never said to be stopped; a failed database update never says the
data is as it was; an upgrade that installs says nothing of being stopped.
"""
from __future__ import annotations

import pytest

cli = pytest.importorskip("synapse.cli")

STILL_STOPPED = "Synapse was stopped for the upgrade and is still stopped."
DATA_AS_IT_WAS = "Your data is as it was; start it again with: synapse start"
START_IT = "Start it again with: synapse start"


def _pip_upgrade(monkeypatch, tmp_path, capsys, *, running: bool, failing: str | None):
    """`_upgrade_with_pip` with a Synapse `running` before it, pip's command `failing` at "install" or "migrate"."""
    from synapse.installer import runner

    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setattr(cli, "_installed_extras", lambda: [])
    monkeypatch.setattr(cli, "_read_pidfile", lambda path: 4242 if running else None)
    monkeypatch.setattr(cli, "_is_running", lambda pid: running)
    monkeypatch.setattr(cli, "_stop_command", lambda: None)
    monkeypatch.setattr(cli, "_var_dir", lambda: tmp_path / "var")
    started: list = []

    def run(cmd, log, **kw):
        joined = " ".join(str(c) for c in cmd)
        if failing == "install" and " pip install " in f" {joined} ":
            raise runner.CommandFailed(cmd, 1, ["ERROR: No matching distribution found for synapse-orch-ai==2.0.16"])
        if failing == "migrate" and " migrate " in f" {joined} ":
            raise runner.CommandFailed(cmd, 1, ["a migration stopped"])
        if " start " in f" {joined} ":
            started.append(cmd)
        return 0, ""

    monkeypatch.setattr(runner, "run", run)
    code = cli._upgrade_with_pip("2.0.15", "2.0.16")
    return code, capsys.readouterr().out, started


def _said_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if STILL_STOPPED in line or START_IT.lower() in line.lower()]


class TestAFailedPipUpgradeSaysItIsStillStopped:
    def test_the_install_failed(self, monkeypatch, tmp_path, capsys):
        code, out, started = _pip_upgrade(monkeypatch, tmp_path, capsys, running=True, failing="install")
        assert code == 1 and started == [], out
        assert STILL_STOPPED in out and DATA_AS_IT_WAS in out, out
        assert all(len(line) <= 80 for line in _said_lines(out)), _said_lines(out)

    def test_the_database_update_failed_never_says_the_data_is_as_it_was(self, monkeypatch, tmp_path, capsys):
        code, out, _ = _pip_upgrade(monkeypatch, tmp_path, capsys, running=True, failing="migrate")
        assert code == 1 and STILL_STOPPED in out and START_IT in out, out
        assert "Your data is as it was" not in out, out


class TestNearMisses:
    def test_not_running_before_says_nothing_of_being_stopped(self, monkeypatch, tmp_path, capsys):
        code, out, _ = _pip_upgrade(monkeypatch, tmp_path, capsys, running=False, failing="install")
        assert code == 1 and STILL_STOPPED not in out, out

    def test_an_upgrade_that_installs_says_nothing_of_being_stopped(self, monkeypatch, tmp_path, capsys):
        code, out, _ = _pip_upgrade(monkeypatch, tmp_path, capsys, running=True, failing=None)
        assert STILL_STOPPED not in out, out
