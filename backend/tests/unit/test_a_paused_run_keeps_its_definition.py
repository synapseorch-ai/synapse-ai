"""A paused run carries on with the workflow it started with (F443).

IDEA1 measured it on the fleet: Human → Print("v1") → End, the Print edited to
"v2" while the run waited, the answer given — and the run printed "v2". Every
resume read the workflow as saved now: `engine.resume` through
`resolve_orchestration`, the fleet's answer job through `_load_orchestration`.
Deleting a step while the run waited had never been tried.

A run keeps a copy now, written once when it starts, and every resume nobody
chose carries on with it: an answer, a tool approval, a nested sub-run, a job
handed out again. A person's Replay is the one exception — the failure card
says "edit, then Replay" — and the copy becomes what it ran.
"""
import time

from _fakes import seed as S
from tests.unit.test_a_failed_child_fails_its_parent import (  # noqa: F401 — fixtures
    _broken, _ctx, _human, _print, _run, _server, driver, worker,
)
from tests.unit.test_a_root_reads_running_while_its_child_works import _call


def _workflow(orch_id, said="v1", *, steps=None, entry="ask", **extra):
    """Human → Print(said) → End, IDEA1's probe."""
    return S.make_orchestration(id=orch_id, entry_step_id=entry, steps=steps or [
        _human("ask", nxt="say"),
        _print("say", said, nxt="done", key="said"),
        {"id": "done", "name": "Done", "type": "end"},
    ], **extra)


async def _save(orch):
    from core.store.resources import save_orchestration
    await save_orchestration(orch)


async def _stored_definition(run_id):
    from sqlalchemy import select
    from core.store import session
    from core.store.models import OrchestrationRunDB
    async with session() as s:
        return (await s.execute(
            select(OrchestrationRunDB.definition).where(OrchestrationRunDB.run_id == run_id)
        )).scalar_one()


async def _set_stored_definition(run_id, value):
    from sqlalchemy import update
    from core.store import session
    from core.store.models import OrchestrationRunDB
    async with session() as s:
        await s.execute(update(OrchestrationRunDB)
                        .where(OrchestrationRunDB.run_id == run_id).values(definition=value))
        await s.commit()


def _step(definition, step_id):
    return next((s for s in definition["steps"] if s["id"] == step_id), None)


class TestAnAnswerCarriesOnWithTheCopy:
    """In-process (`OrchestrationEngine.resume`) and on the fleet (the answer job)."""

    async def test_an_edit_during_the_pause_is_not_followed(self, driver):
        """IDEA1's probe."""
        orch = _workflow("orch_f443_edit")
        await S.seed_orchestrations([orch])
        assert await driver.start(orch, "run_f443_edit") == "paused"

        await _save(_workflow("orch_f443_edit", "v2"))
        assert await driver.answer("run_f443_edit", "approve") == "completed"

        assert (await _run("run_f443_edit")).shared_state["said"] == "v1"

    async def test_a_step_deleted_during_the_pause_still_runs(self, driver):
        orch = _workflow("orch_f443_gone")
        await S.seed_orchestrations([orch])
        assert await driver.start(orch, "run_f443_gone") == "paused"

        await _save(_workflow("orch_f443_gone", steps=[
            _human("ask", nxt="done"), {"id": "done", "name": "Done", "type": "end"}]))
        assert await driver.answer("run_f443_gone", "approve") == "completed"

        assert (await _run("run_f443_gone")).shared_state["said"] == "v1"

    async def test_the_step_it_waits_at_deleted_during_the_pause(self, driver):
        """The answer lands on the step that asked, and the run goes on past it."""
        orch = _workflow("orch_f443_asker")
        await S.seed_orchestrations([orch])
        assert await driver.start(orch, "run_f443_asker") == "paused"

        await _save(_workflow("orch_f443_asker", entry="say", steps=[
            _print("say", "v2", nxt="done", key="said"), {"id": "done", "name": "Done", "type": "end"}]))
        assert await driver.answer("run_f443_asker", "approve") == "completed"

        run = await _run("run_f443_asker")
        assert run.shared_state["approval"] == "approve"
        assert run.shared_state["said"] == "v1"
        ask = [h for h in run.step_history if h["step_id"] == "ask"]
        assert [h["status"] for h in ask] == ["completed"]

    async def test_the_workflow_deleted_during_the_pause(self, driver):
        """It used to fail "not found" in-process and dead-letter on the fleet."""
        from core.store.resources import delete_orchestration
        orch = _workflow("orch_f443_deleted")
        await S.seed_orchestrations([orch])
        assert await driver.start(orch, "run_f443_deleted") == "paused"

        await delete_orchestration("orch_f443_deleted")
        assert await driver.answer("run_f443_deleted", "approve") == "completed"

        assert (await _run("run_f443_deleted")).shared_state["said"] == "v1"

    async def test_a_run_from_before_the_copy_follows_the_saved_workflow(self, driver):
        """The only definition there is for it: as every resume did before F443."""
        orch = _workflow("orch_f443_old")
        await S.seed_orchestrations([orch])
        assert await driver.start(orch, "run_f443_old") == "paused"
        await _set_stored_definition("run_f443_old", None)

        await _save(_workflow("orch_f443_old", "v2"))
        assert await driver.answer("run_f443_old", "approve") == "completed"

        assert (await _run("run_f443_old")).shared_state["said"] == "v2"

    async def test_a_copy_that_no_longer_loads_follows_the_saved_workflow(self, driver):
        """An upgraded engine that refuses the copy must not strand the run."""
        orch = _workflow("orch_f443_bad")
        await S.seed_orchestrations([orch])
        assert await driver.start(orch, "run_f443_bad") == "paused"
        await _set_stored_definition("run_f443_bad", {"id": "orch_f443_bad", "steps": "not a list"})

        await _save(_workflow("orch_f443_bad", "v2"))
        assert await driver.answer("run_f443_bad", "approve") == "completed"

        assert (await _run("run_f443_bad")).shared_state["said"] == "v2"


class TestANestedSubRunCarriesOnWithItsCopy:
    async def test_neither_level_follows_an_edit(self, driver):
        """The parent's next step, the sub-workflow's steps and its result key were
        all edited while the sub-run waited; each level runs as it started."""
        sub = S.make_orchestration(id="orch_f443_sub", name="Sub", entry_step_id="ask", result_key="said", steps=[
            _human("ask", nxt="say"),
            _print("say", "s1", nxt="tail", key="said"),
            _print("tail", "tail", key="tail_k"),
        ])
        parent = S.make_orchestration(id="orch_f443_parent", name="Parent", entry_step_id="call", steps=[
            _call("agent_f443_sub", "call", nxt="after"),
            _print("after", "p1", key="after"),
        ])
        await S.seed_agents([S.make_orchestrator_agent(sub["id"], id="agent_f443_sub")])
        await S.seed_orchestrations([parent, sub])
        assert await driver.start(parent, "run_f443_nest") == "paused"

        await _save({**sub, "result_key": "tail_k", "steps": [
            _human("ask", nxt="say"), _print("say", "s2", nxt="tail", key="said"),
            _print("tail", "tail", key="tail_k")]})
        await _save({**parent, "steps": [_call("agent_f443_sub", "call", nxt="after"),
                                         _print("after", "p2", key="after")]})
        assert await driver.answer("run_f443_nest", "approve") == "completed"

        run = await _run("run_f443_nest")
        assert run.shared_state["after"] == "p1"
        # The sub-run's own copy names its result: "said", not the edit's "tail_k".
        assert run.shared_state["out_call"] == "s1"

    async def test_its_workflow_deleted_during_the_pause(self, driver):
        """The sub-run finishes on its copy, and its parent with it. (A sub-run from
        before the copy, with its workflow gone, still fails its parent —
        `test_human_in_containers.py`.)"""
        from core.store.resources import delete_orchestration
        sub = S.make_orchestration(id="orch_f443_subgone", name="Sub", entry_step_id="ask", result_key="said",
                                   steps=[_human("ask", nxt="say"), _print("say", "s1", key="said")])
        parent = S.make_orchestration(id="orch_f443_pgone", name="Parent", entry_step_id="call", steps=[
            _call("agent_f443_subgone", "call", nxt="after"), _print("after", "p1", key="after")])
        await S.seed_agents([S.make_orchestrator_agent(sub["id"], id="agent_f443_subgone")])
        await S.seed_orchestrations([parent, sub])
        assert await driver.start(parent, "run_f443_subgone") == "paused"

        await delete_orchestration("orch_f443_subgone")
        assert await driver.answer("run_f443_subgone", "approve") == "completed"

        run = await _run("run_f443_subgone")
        assert run.shared_state["out_call"] == "s1"
        assert run.shared_state["after"] == "p1"


class TestAToolApprovalCarriesOnWithTheCopy:
    async def test_the_step_after_it_is_not_the_edit(self, fake_llm):
        from tests.unit.test_tool_approval_resume import _orch, _resume, _run as _run_until_asked
        from _fakes.fake_llm import tool_call

        orch = _orch()
        await S.seed_orchestrations([orch])
        await S.seed_agents([S.make_agent(
            id="risky_agent", tools=["all"], skip_default_tools=True,
            tool_policy={"undeclared": "confirm"},
        )])
        executed = []
        await _run_until_asked(orch, "run_f443_tool", fake_llm, [tool_call("danger"), "done"], executed)

        edited = {**orch, "steps": [orch["steps"][0], {**orch["steps"][1], "print_content": "edited"}]}
        await _save(edited)
        await _resume("run_f443_tool", {"approve": True}, fake_llm, ["all finished"], executed)

        run = await _run("run_f443_tool")
        assert executed == [("danger", {})]
        assert run.status == "completed"
        assert run.shared_state["tail"] == "done"


def _failing(orch_id):
    """A step that fails, then Print("v1")."""
    return S.make_orchestration(id=orch_id, entry_step_id="broken", steps=[
        _broken("broken", nxt="say"), _print("say", "v1", key="said")])


def _fixed(orch_id, said="v2"):
    return S.make_orchestration(id=orch_id, entry_step_id="broken", steps=[
        _print("broken", "fixed", nxt="say", key="fixed"), _print("say", said, key="said")])


class TestAPersonsReplayTakesTheSavedWorkflow:
    """The failure card says "edit, then Replay"; a Replay that ran the copy
    would fail the same way. What it ran becomes the run's copy."""

    async def test_in_process(self):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        orch = _failing("orch_f443_replay")
        await S.seed_orchestrations([orch])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        [e async for e in engine.run("go", run_id="run_f443_replay")]
        assert (await _run("run_f443_replay")).status == "failed"

        await _save(_fixed("orch_f443_replay"))
        [e async for e in OrchestrationEngine.resume_failed("run_f443_replay", _server())]

        run = await _run("run_f443_replay")
        assert run.status == "completed"
        assert run.shared_state["said"] == "v2"
        assert _step(await _stored_definition("run_f443_replay"), "broken")["type"] == "print"

    async def test_on_the_fleet(self, worker, fake_redis):
        orch = _failing("orch_f443_replay_w")
        await S.seed_orchestrations([orch])
        await worker.run_orchestration_job(
            await _ctx(fake_redis), run_id="run_f443_replay_w", orch_id=orch["id"], initial_input="go")
        assert (await _run("run_f443_replay_w")).status == "failed"

        await _save(_fixed("orch_f443_replay_w"))
        result = await worker.resume_failed_job(await _ctx(fake_redis), run_id="run_f443_replay_w")

        assert result["status"] == "completed"
        assert (await _run("run_f443_replay_w")).shared_state["said"] == "v2"
        assert _step(await _stored_definition("run_f443_replay_w"), "broken")["type"] == "print"

    async def test_a_later_answer_carries_on_with_what_the_replay_ran(self):
        """The copy is re-stamped, not left as the start's: a question asked after
        the Replay is answered on the workflow the Replay took."""
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        def version(first, said):
            return S.make_orchestration(id="orch_f443_restamp", entry_step_id="broken", steps=[
                first, _human("ask", nxt="say"), _print("say", said, key="said")])

        orch = version(_broken("broken", nxt="ask"), "v1")
        await S.seed_orchestrations([orch])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        [e async for e in engine.run("go", run_id="run_f443_restamp")]

        await _save(version(_print("broken", "fixed", nxt="ask", key="fixed"), "v2"))
        events = [e async for e in OrchestrationEngine.resume_failed("run_f443_restamp", _server())]
        assert events[-1]["type"] == "human_input_required"

        await _save(version(_print("broken", "fixed", nxt="ask", key="fixed"), "v3"))
        [e async for e in OrchestrationEngine.resume("run_f443_restamp", "yes", _server())]

        assert (await _run("run_f443_restamp")).shared_state["said"] == "v2"


class TestAJobHandedOutAgainCarriesOnWithTheCopy:
    """Nobody chose anything: a worker stopped, and the job came back."""

    async def test_in_process(self):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        orch = _failing("orch_f443_again")
        await S.seed_orchestrations([orch])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        [e async for e in engine.run("go", run_id="run_f443_again")]

        await _save(_fixed("orch_f443_again"))
        [e async for e in OrchestrationEngine.resume_failed("run_f443_again", _server(), interrupted=True)]

        run = await _run("run_f443_again")
        assert run.status == "failed"   # the step it stopped at ran again, as it started
        assert "fixed" not in run.shared_state
        assert _step(await _stored_definition("run_f443_again"), "broken")["type"] == "tool"

    async def test_a_replay_job_handed_out_again(self, worker, fake_redis):
        from tests.unit.test_dead_letter import _ctx as _ctx_try
        orch = _failing("orch_f443_again_w")
        await S.seed_orchestrations([orch])
        await worker.run_orchestration_job(
            await _ctx(fake_redis), run_id="run_f443_again_w", orch_id=orch["id"], initial_input="go")

        await _save(_fixed("orch_f443_again_w"))
        result = await worker.resume_failed_job(await _ctx_try(fake_redis, job_try=2), run_id="run_f443_again_w")

        assert result["status"] == "failed"
        assert "fixed" not in (await _run("run_f443_again_w")).shared_state


class TestTheCopyIsWrittenOnce:
    async def test_it_is_the_workflow_as_the_run_started(self, driver):
        orch = _workflow("orch_f443_kept")
        await S.seed_orchestrations([orch])
        assert await driver.start(orch, "run_f443_kept") == "paused"

        kept = await _stored_definition("run_f443_kept")
        assert kept["id"] == "orch_f443_kept"
        assert _step(kept, "say")["print_content"] == "v1"
        assert (await _run("run_f443_kept")).definition == kept

    async def test_a_checkpoint_after_the_first_does_not_rewrite_it(self):
        """A step boundary's full save leaves the copy alone; only `keep_definition`
        (the start, a Replay) puts one in."""
        from core.orchestration.state import SharedState
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        orch = _workflow("orch_f443_once")
        await S.seed_orchestrations([orch])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        [e async for e in engine.run("go", run_id="run_f443_once")]

        run = await _run("run_f443_once")
        run.definition = {"id": "orch_f443_once", "name": "changed in memory", "steps": []}
        await SharedState(run).checkpoint()
        assert (await _stored_definition("run_f443_once"))["name"] != "changed in memory"

        run.keep_definition({"id": "orch_f443_once", "name": "kept", "steps": []})
        await SharedState(run).checkpoint()
        assert (await _stored_definition("run_f443_once"))["name"] == "kept"

    async def test_a_partial_checkpoint_never_carries_it(self):
        from core.orchestration.state import SharedState
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        orch = _workflow("orch_f443_partial")
        await S.seed_orchestrations([orch])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        [e async for e in engine.run("go", run_id="run_f443_partial")]

        run = await _run("run_f443_partial")
        run.keep_definition({"id": "orch_f443_partial", "name": "pending", "steps": []})
        await SharedState(run).checkpoint(columns=("step_progress",))
        assert (await _stored_definition("run_f443_partial"))["name"] != "pending"

    async def test_listings_do_not_read_it(self):
        from core.orchestration.state import SharedState
        from core.orchestration.state_store import list_children
        orch = _workflow("orch_f443_list")
        await S.seed_orchestrations([orch])
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        [e async for e in engine.run("go", run_id="run_f443_list")]

        listed = [r for r in await SharedState.list_runs(limit=200) if r["run_id"] == "run_f443_list"]
        assert listed and "definition" not in listed[0]
        assert await list_children("run_f443_list") == []


class TestTheRunDetailCarriesTheCopyOnlyWhenAsked:
    """It is a whole definition (the builder's is ~60 KB), and the detail is
    polled; the run page asks once."""

    async def _paused(self):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        orch = _workflow("orch_f443_view")
        await S.seed_orchestrations([orch])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        [e async for e in engine.run("go", run_id="run_f443_view")]

    async def test_the_app_route(self, client):
        await self._paused()
        plain = (await client.get("/api/orchestrations/runs/run_f443_view")).json()
        assert "definition" not in plain

        asked = (await client.get("/api/orchestrations/runs/run_f443_view?definition=1")).json()
        assert _step(asked["definition"], "say")["print_content"] == "v1"
        assert asked["status"] == plain["status"] == "paused"

    async def test_v1_and_mcp_reads_never(self):
        from core.orchestration.run_view import read_run
        await self._paused()
        assert "definition" not in await read_run("run_f443_view")


class TestAChannelsWindowIsTheCopys:
    async def test_an_edit_during_the_pause_does_not_move_the_window(self):
        from core.messaging.manager import question_expired
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        def briefly(seconds):
            orch = _workflow("orch_f443_window")
            orch["steps"][0]["human_timeout_seconds"] = seconds
            return orch

        orch = briefly(60)
        await S.seed_orchestrations([orch])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        [e async for e in engine.run("go", run_id="run_f443_window")]

        await _save(briefly(10_000_000))
        assert await question_expired("run_f443_window", now=time.time() + 600) is True


class TestTheColumnIsAddedToAnExistingInstall:
    async def test_by_the_startup_migration(self, tmp_path):
        from sqlalchemy import inspect, text
        from sqlalchemy.ext.asyncio import create_async_engine
        from core.store.migrate import run_migrations

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE orchestration_runs ("
                "  run_id VARCHAR(255) PRIMARY KEY, orchestration_id VARCHAR(255) NOT NULL,"
                "  tenant_id VARCHAR(255) NOT NULL DEFAULT 'default', status VARCHAR(50) NOT NULL,"
                "  shared_state JSON NOT NULL, step_history JSON NOT NULL)"
            ))

        applied = await run_migrations(engine)
        assert "orchestration_runs.definition" in applied
        async with engine.begin() as conn:
            columns = await conn.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns("orchestration_runs")})
        assert "definition" in columns
        await engine.dispose()

