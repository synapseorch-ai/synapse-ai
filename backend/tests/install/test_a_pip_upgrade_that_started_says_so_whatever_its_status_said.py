"""A pip upgrade that installed, updated the database and started Synapse says so, whatever its status read said (F1642).

After a pip upgrade starts the new version, it reads `python -m synapse status --json` for the link it ends with. It
took the last line of that output with no guard: a status that printed nothing ended the upgrade in a Python
traceback, and a last line that was not JSON (a warning after it) marked "Starting" failed and said "Upgrade failed"
about an upgrade that had worked; nothing bounded how long it waited. Now the status is read as the installer reads
its own CLI: the last line that is JSON, within a time limit. With no usable answer, the upgrade says the version is
installed and was started, and how to check it, never that it failed.

Near misses: a status that answers JSON gives the link as before, even with a line after it; a Synapse that was not
running before is told how to start it, as before; an install that really failed still says the upgrade failed.
"""
from __future__ import annotations

import subprocess

import pytest

cli = pytest.importorskip("synapse.cli")

LINK = "http://localhost:3000/?token=tok"


def _upgrade(monkeypatch, tmp_path, capsys, *, status, running=True, failing=None, target="2.0.16"):
    """`_upgrade_with_pip` to `target` with a Synapse `running` before it; `status` is what the status read prints,
    or an exception it raises."""
    from synapse.installer import runner

    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setattr(cli, "_installed_extras", lambda: [])
    monkeypatch.setattr(cli, "_read_pidfile", lambda path: 4242 if running else None)
    monkeypatch.setattr(cli, "_is_running", lambda pid: running)
    monkeypatch.setattr(cli, "_stop_command", lambda: None)
    monkeypatch.setattr(cli, "_var_dir", lambda: tmp_path / "var")

    def run(cmd, log, **kw):
        if failing == "install" and " pip install " in f" {' '.join(str(c) for c in cmd)} ":
            raise runner.CommandFailed(cmd, 1, ["ERROR: No matching distribution found for synapse-orch-ai"])
        return 0, ""

    monkeypatch.setattr(runner, "run", run)
    asked: list[dict] = []
    real = cli.subprocess.run

    def status_read(cmd, *a, **kw):
        if [str(c) for c in cmd][-3:] == ["synapse", "status", "--json"]:
            asked.append(kw)
            if isinstance(status, BaseException):
                raise status
            return cli.subprocess.CompletedProcess(cmd, 0, stdout=status, stderr="")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(cli.subprocess, "run", status_read)
    code = cli._upgrade_with_pip("2.0.15", target)
    return code, capsys.readouterr().out, asked


def _installed_and_started(version: str) -> str:
    """Two lines, each inside 80 columns with the screen's two-space indent (a plain terminal prints them without it)."""
    return (f"Synapse {version} is installed and was started,\n"
            "but it didn't say where it is running. Check it with: synapse status")


class TestNoUsableStatusIsNeverAFailure:
    @pytest.mark.parametrize("status", ["", "\n\n", "Traceback (most recent call last):\nboom\n",
                                        "a status warning, not JSON\n"])
    def test_it_says_the_version_is_installed_and_was_started(self, monkeypatch, tmp_path, capsys, status):
        code, out, _ = _upgrade(monkeypatch, tmp_path, capsys, status=status)
        assert code == 0, out
        assert _installed_and_started("2.0.16") in out and "Upgrade failed" not in out, out
        assert all(len("  " + line) <= 80 for line in _installed_and_started("2.0.16").splitlines())

    def test_a_status_that_does_not_answer_in_time(self, monkeypatch, tmp_path, capsys):
        code, out, asked = _upgrade(monkeypatch, tmp_path, capsys,
                                    status=subprocess.TimeoutExpired(["synapse", "status"], 30))
        assert code == 0 and _installed_and_started("2.0.16") in out and "Upgrade failed" not in out, out
        assert asked and asked[0].get("timeout"), asked

    def test_the_line_names_the_version_it_installed(self, monkeypatch, tmp_path, capsys):
        code, out, _ = _upgrade(monkeypatch, tmp_path, capsys, status="", target="2.0.17")
        assert _installed_and_started("2.0.17") in out, out


class TestNearMisses:
    def test_a_status_that_answers_gives_the_link(self, monkeypatch, tmp_path, capsys):
        code, out, _ = _upgrade(monkeypatch, tmp_path, capsys, status=f'{{"link": "{LINK}"}}\n')
        assert code == 0 and "Synapse 2.0.16 is running" in out and LINK in out, out

    def test_a_line_after_the_json_still_gives_the_link(self, monkeypatch, tmp_path, capsys):
        code, out, _ = _upgrade(monkeypatch, tmp_path, capsys,
                                status=f'{{"link": "{LINK}"}}\na status warning after it\n')
        assert code == 0 and LINK in out and "Upgrade failed" not in out, out

    def test_not_running_before_is_told_how_to_start_it(self, monkeypatch, tmp_path, capsys):
        code, out, asked = _upgrade(monkeypatch, tmp_path, capsys, status="", running=False)
        assert code == 0 and "Synapse 2.0.16 is installed. Start it with: synapse start" in out and asked == [], out

    def test_an_install_that_failed_still_says_so(self, monkeypatch, tmp_path, capsys):
        code, out, _ = _upgrade(monkeypatch, tmp_path, capsys, status="", failing="install")
        assert code == 1 and "Upgrade failed" in out, out
