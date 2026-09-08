"""
A paused run resumes on a process that never saw it start.

This is security finding #5 and the second half of the verification bar. Run
state used to live in `backend/logs/orchestration_runs/{run_id}.json`, on the
local disk of whichever process happened to be executing — so a resume job
dispatched by ARQ to a different worker hit `FileNotFoundError`, because a queue
has no affinity. `SharedStatePG` was imported and never instantiated; both its
docstring and the adapter's header claimed the state was in Postgres, and it was
on local disk in *both* modes.

`test_orchestration_engine_deep.py` covers pause-then-resume, but in one process,
one event loop, one set of module globals. That passes whether or not the state
ever leaves memory. So this one resumes in a genuinely separate OS process,
which needs no Postgres: the conftest store is a file-backed SQLite with WAL and
`NullPool`, and a child handed the same `SYNAPSE_DB_URL` reaches the same rows.
`tests/unit/test_no_data_dir.py` established the idiom.

Worth noting what is *not* here. On the pro side the equivalent test has to
delete a local checkpoint file first, "otherwise the file left behind by the
first would satisfy `restore()` and the test would pass without the shuttle
doing anything". There is no file to delete any more, so instead this asserts
that nothing local could have satisfied the restore — the inverted form of the
same precaution.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _fakes import seed as S

_BACKEND = Path(__file__).resolve().parent.parent.parent

#: print -> human -> print, so there is history on *both* sides of the pause and
#: the crossing is observable from either end.
PAUSING = dict(
    id="orch_cross_process",
    entry_step_id="before",
    steps=[
        {"id": "before", "name": "Log the request", "type": "print",
         "print_content": "Received {state.ticket}",
         "output_key": "logged", "next_step_id": "h"},
        {"id": "h", "name": "Approve", "type": "human",
         "human_prompt": "OK to proceed?",
         "human_fields": [{"name": "decision", "type": "text", "label": "Decision"}],
         "output_key": "approval", "input_keys": [], "next_step_id": "after"},
        {"id": "after", "name": "Show", "type": "print",
         "print_content": "Decision was: {state.approval}",
         "output_key": "final", "next_step_id": None},
    ],
)

#: Resumes the run and reports what came back. Runs in a *different* process,
#: sharing only the environment: the database URL, the blob dir, the state dir.
_CHILD = """
import asyncio, json, sys, types


async def main(run_id):
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.state import SharedState

    server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
    events = [ev async for ev in OrchestrationEngine.resume(run_id, {"decision": "yes"}, server)]
    run = (await SharedState.restore(run_id)).run
    print("RESULT" + json.dumps({
        "types": [e.get("type") for e in events],
        "finals": [str(e.get("response", "")) for e in events if e.get("type") == "final"],
        "status": run.status,
        "shared_state": run.shared_state,
        "step_ids": [s.get("step_id") for s in run.step_history],
        "step_history": run.step_history,
        "waiting_for_human": run.waiting_for_human,
    }))


asyncio.run(main(sys.argv[1]))
"""


#: Resumes a FAILED run in another process with run_agent_step stubbed, and
#: reports the resume_state the stub was handed — i.e. whether the mid-step
#: transcript checkpointed by the parent process reached the child intact.
_CHILD_FAILED = """
import asyncio, json, sys, types


async def main(run_id):
    import core.react_engine as re_mod
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.state import SharedState

    seen = {}

    async def resumed(**kwargs):
        seen["resume_state"] = kwargs.get("resume_state")
        yield {"type": "final", "response": "done after crossing"}

    re_mod.run_agent_step = resumed
    server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
    events = [ev async for ev in OrchestrationEngine.resume_failed(run_id, server)]
    run = (await SharedState.restore(run_id)).run
    print("RESULT" + json.dumps({
        "types": [e.get("type") for e in events],
        "resume_state": seen.get("resume_state"),
        "status": run.status,
        "step_progress": run.step_progress,
        "shared_state": run.shared_state,
    }))


asyncio.run(main(sys.argv[1]))
"""


def _spawn(run_id: str, db_url: str | None = None, child: str = _CHILD) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_BACKEND)
    for var in ("SYNAPSE_DB_URL", "SYNAPSE_BLOB_DIR", "SYNAPSE_SCRATCH_DIR", "SYNAPSE_STATE_DIR"):
        assert var in env, f"{var} is not set; the child would not find the same store"
    if db_url is not None:
        env["SYNAPSE_DB_URL"] = db_url

    return subprocess.run(
        [sys.executable, "-c", child, run_id],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _in_another_process(run_id: str, child: str = _CHILD) -> dict:
    result = _spawn(run_id, child=child)
    assert result.returncode == 0, f"child failed:\n{result.stdout}\n{result.stderr}"

    marker = [line for line in result.stdout.splitlines() if line.startswith("RESULT")]
    assert marker, f"child produced no result:\n{result.stdout}\n{result.stderr}"
    return json.loads(marker[-1][len("RESULT"):])


async def _pause_a_run(run_id: str) -> None:
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    import types

    await S.seed_orchestrations([S.make_orchestration(**PAUSING)])
    orch = Orchestration.model_validate(S.make_orchestration(**PAUSING))
    server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})

    events = [
        ev
        async for ev in OrchestrationEngine(orch, server).run(
            "the task", run_id=run_id, initial_state={"ticket": "SYN-1"}
        )
    ]
    assert any(e.get("type") == "human_input_required" for e in events)
    assert all(e.get("type") != "orchestration_complete" for e in events)


class TestAPausedRunIsPortable:
    async def test_it_resumes_in_a_different_process(self, fake_llm):
        await _pause_a_run("run_portable")

        result = _in_another_process("run_portable")

        assert "orchestration_complete" in result["types"], result["types"]
        assert result["status"] == "completed"
        assert result["waiting_for_human"] is False

    async def test_shared_state_and_step_history_survive_the_crossing(self, fake_llm):
        """The two columns that *are* the run, not a summary of it."""
        await _pause_a_run("run_state_survives")

        result = _in_another_process("run_state_survives")

        # Written before the pause, in the first process.
        assert result["shared_state"].get("ticket") == "SYN-1"
        assert result["shared_state"].get("user_input") == "the task"
        assert result["shared_state"].get("logged") is not None
        # Written by the resume, in the second.
        assert result["shared_state"].get("approval") == {"decision": "yes"}
        assert "yes" in " ".join(result["finals"])
        # And the history spans both processes: `before` was recorded by the
        # process that paused, `after` by the process that resumed — and the
        # human step's own entry was *opened* by the first and *closed* by the
        # second, which is the crossing at its narrowest.
        assert result["step_ids"] == ["before", "h", "after"], result["step_ids"]
        human = result["step_history"][1]
        assert human["status"] == "completed"
        assert human["started_at"] and human["ended_at"]

    def test_nothing_local_could_have_satisfied_the_restore(self, tmp_path):
        """The inverted form of "delete the checkpoint file first".

        On the pro side that deletion is what stops a stale local file making
        the test pass without the state ever moving. Here the assertion is that
        no such file exists to begin with — if one appears, this test is the
        thing that notices, and the cross-process tests above stop proving
        anything the moment it does.
        """
        state_dir = Path(os.environ["SYNAPSE_STATE_DIR"])
        stray = [
            p
            for p in state_dir.rglob("*")
            if p.is_file() and p.suffix in {".json", ".pkl"} and "run" in p.name
        ]
        assert not stray, (
            "run state reappeared on local disk, which is what made a paused run "
            f"resumable only on the process that paused it: {stray}"
        )

    async def test_the_child_is_reading_the_shared_store_and_nothing_else(self, fake_llm, tmp_path):
        """The control for the two tests above.

        They would pass just as happily if the child were satisfied by something
        it inherited or found locally, which is precisely the failure mode this
        whole change exists to remove. Point the same child at a different
        database and it must not find the run at all.
        """
        await _pause_a_run("run_control")

        elsewhere = _spawn(
            "run_control", db_url=f"sqlite+aiosqlite:///{tmp_path / 'somewhere-else.db'}"
        )

        assert elsewhere.returncode != 0, (
            "the child resumed a run that is not in the database it was pointed at, "
            "so these tests are not proving the state crossed the process boundary:\n"
            f"{elsewhere.stdout}"
        )
        assert "No checkpoint found" in elsewhere.stderr, elsewhere.stderr

    async def test_a_run_that_never_paused_cannot_be_resumed(self, fake_llm):
        """The negative: `restore` raising is what a wrong id looks like."""
        from core.orchestration.state import SharedState

        with pytest.raises(FileNotFoundError):
            await SharedState.restore("run_that_does_not_exist")


class TestMidStepResumeCrossesProcesses:
    async def test_the_transcript_checkpointed_here_resumes_there(self, monkeypatch, seed_orchestration):
        """The parent crashes after one tool turn; a different process resumes
        the failed run and must be handed that turn's transcript — which now
        lives in its own column, and must be there for the child to read."""
        import types
        import core.react_engine as re_mod
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        orch = S.make_orchestration(
            id="orch_cross_mid", entry_step_id="s1",
            steps=[{"id": "s1", "name": "Agent", "type": "agent", "agent_id": "a1",
                    "output_key": "result", "next_step_id": None, "max_turns": 5}],
        )
        await seed_orchestration(**orch)

        async def crashing(**kwargs):
            yield {"type": "_step_progress", "turn": 1, "context_text": "CTX-FROM-PARENT",
                   "has_compacted": False, "browser_actions": [], "tools_used": []}
            raise RuntimeError("parent died")

        monkeypatch.setattr(re_mod, "run_agent_step", crashing)
        server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
        engine = OrchestrationEngine(Orchestration.model_validate(orch), server)
        events = [ev async for ev in engine.run("go", run_id="run_cross_mid")]
        assert any(e.get("type") == "step_error" for e in events)

        child = _in_another_process("run_cross_mid", child=_CHILD_FAILED)

        assert child["resume_state"]["context_text"] == "CTX-FROM-PARENT"
        assert child["status"] == "completed"
        assert child["step_progress"] == {}                       # cleaned up after success
        assert "_step_progress_s1" not in child["shared_state"]
        assert child["shared_state"]["result"] == "done after crossing"
