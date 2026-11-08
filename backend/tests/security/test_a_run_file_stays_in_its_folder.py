"""A run's log and journal are files in their own folder, whatever id names them (F1479, CodeQL py/path-injection).

The agent and workflow logs (`<run_id>.log`) and a run's event journal (`<run_id>.jsonl`) are named from the run's id,
and the log and events routes take that id from the URL. A path parameter never holds `/`, but on Windows `\\` is a
separator and `C:\\x` replaces the folder: `..\\..\\x` read or deleted any `.log` the server could reach. Each one is
now made by `core.run_ids.run_file`, which keeps it in its folder. Windows' rules are checked here with `ntpath`.
"""
from __future__ import annotations

import ntpath
import posixpath

import pytest

from core.run_ids import run_file

CLIMBING = [r"..\..\x", r"..\x", r"C:\x", r"C:x", "../x", "../../x", "..", ".", "", "a\x00b", "a:b", "/etc/x"]


@pytest.mark.parametrize("flavour, base", [(ntpath, r"C:\synapse\logs"), (posixpath, "/srv/synapse/logs")])
@pytest.mark.parametrize("run_id", CLIMBING)
def test_an_id_that_climbs_out_names_a_file_in_the_folder(flavour, base, run_id):
    path = run_file(base, run_id, ".log", _path=flavour)
    assert flavour.dirname(str(path)) == flavour.realpath(base)


def test_the_way_out_it_closes():
    """What `<folder> / f"{run_id}.log"` was on Windows, for the record."""
    assert ntpath.normpath(ntpath.join(r"C:\synapse\logs", r"..\..\x.log")) == r"C:\x.log"
    assert ntpath.join(r"C:\synapse\logs", r"C:\x.log") == r"C:\x.log"


@pytest.mark.parametrize("run_id", ["run_wf_daily_1762600000000_a1b2c3", "agentrun_default_1762600000000_a1b2c3",
                                    "run_wf_1762600000000_a1b2c3__step_2_d1_0f0f0f"])
def test_an_ordinary_id_keeps_its_own_name(tmp_path, run_id):
    assert run_file(tmp_path, run_id, ".log") == (tmp_path / f"{run_id}.log").resolve()


def test_an_id_with_a_colon_is_found_again_where_it_was_kept(tmp_path):
    """A run whose own id holds a refused character still has one file, the same each time."""
    first = run_file(tmp_path, "run_a:b_1762600000000_a1b2c3", ".log")
    assert first == run_file(tmp_path, "run_a:b_1762600000000_a1b2c3", ".log")
    assert first.parent == tmp_path.resolve() and ":" not in first.name


def test_a_file_that_points_out_of_the_folder_is_refused(tmp_path):
    outside = tmp_path / "outside.log"
    outside.write_text("not a run's")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "run_x.log").symlink_to(outside)
    with pytest.raises(ValueError):
        run_file(logs, "run_x", ".log")


# ── every door goes through it ────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def seen(monkeypatch):
    """Each module's `run_file`, spied on: the ids it was asked for."""
    import core.agent_logger as agent_logger
    import core.orchestration.journal as journal
    import core.orchestration.logger as orch_logger

    asked: list[tuple[str, str]] = []
    for module in (agent_logger, orch_logger, journal):
        real = module.run_file

        def spy(directory, run_id, suffix, *, _real=real):
            asked.append((run_id, suffix))
            return _real(directory, run_id, suffix)

        monkeypatch.setattr(module, "run_file", spy)
    return asked


@pytest.mark.parametrize("run_id", [r"..\..\x", r"C:\x"])
def test_every_log_and_journal_door_names_its_file_through_run_file(seen, run_id):
    from core.agent_logger import AgentLogger
    from core.orchestration.journal import FileRunJournal
    from core.orchestration.logger import OrchestrationLogger

    assert AgentLogger.get_log(run_id) is None
    assert AgentLogger.delete_log(run_id) is False
    assert OrchestrationLogger.get_log(run_id) is None
    assert OrchestrationLogger.delete_log(run_id) is False
    assert FileRunJournal.exists(run_id) is False
    assert FileRunJournal.delete(run_id) is False
    assert FileRunJournal(run_id).read() == []
    assert seen.count((run_id, ".log")) >= 4 and seen.count((run_id, ".jsonl")) >= 3


def test_a_new_log_is_made_through_it_too(seen):
    from core.agent_logger import AgentLogger
    from core.orchestration.logger import OrchestrationLogger

    agent_log = AgentLogger(agent_id="agent_x", agent_name="X", session_id="s", source="chat", user_message="hi")
    agent_log.close()
    orch_log = OrchestrationLogger(run_id="run_wf_1762600000000_a1b2c3", orchestration_id="wf",
                                   orchestration_name="WF", user_input="hi")
    orch_log.close()
    assert (agent_log.run_id, ".log") in seen and ("run_wf_1762600000000_a1b2c3", ".log") in seen
    assert orch_log.path.parent.name == "orchestration_logs"


# ── the routes ────────────────────────────────────────────────────────────────────────────────────────────────────

ROUTES = ["/api/logs/agents/{}", "/api/logs/orchestrations/{}", "/api/orchestrations/logs/{}"]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("encoded", ["..%5C..%5Cx", "C%3A%5Cx", "..%5Cx"])
async def test_a_climbing_id_finds_no_log_and_deletes_nothing(client, route, encoded):
    from core.storage.scratch import scratch_root

    sentinel = scratch_root() / "x.log"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("a file beside the logs")
    try:
        assert (await client.get(route.format(encoded))).status_code == 404
        assert (await client.delete(route.format(encoded))).status_code == 404
        assert sentinel.read_text() == "a file beside the logs"
    finally:
        sentinel.unlink(missing_ok=True)


async def test_a_climbing_id_has_no_events(client):
    resp = await client.get("/api/orchestrations/runs/..%5C..%5Cx/events")
    assert resp.status_code in (200, 404)
    assert not (resp.json().get("events") if resp.status_code == 200 else None)
