"""Nested runs can be told apart, found under their parent, and costed with it (F251).

A child's id was `{run}__{step}_d{depth}`, so every Loop iteration and every
retry of the step overwrote one record and one log. Its events reached the
parent relabelled as the parent step, with its own step's name dropped, so the
run log read "Market analysis (L2) completed" when an L3 step finished; its own
`orchestration_error` reached the parent's stream as the parent's. Nothing
listed a run's children, the Usage page showed each child as a run of its own
while the parent's row missed their calls, and an in-process run's cost was
never written at all.

Now each child run has an id of its own (still `{run}__…`, which is what hides
children everywhere), a child's events carry its own step as `nested_step_id`
and `nested_step_name`, a run's children are listed under it, and a run's cost
is its own and its children's.
"""
import pytest

from _fakes import seed as S
from tests.unit.test_a_failed_child_fails_its_parent import (  # noqa: F401 — fixtures
    _human, _print, _run, _server, driver, worker,
)


async def _prefixed(prefix):
    from sqlalchemy import select
    from core.store import session
    from core.store.models import OrchestrationRunDB
    async with session() as s:
        rows = (await s.execute(
            select(OrchestrationRunDB.run_id).where(
                OrchestrationRunDB.run_id.startswith(prefix, autoescape=True))
            .order_by(OrchestrationRunDB.created_at)
        )).scalars().all()
    return list(rows)


def _call(agent_id, sid, nxt=None, name=None, template="go"):
    return {"id": sid, "name": name or f"Call {sid}", "type": "agent", "agent_id": agent_id,
            "prompt_template": template, "output_key": f"out_{sid}", "next_step_id": nxt}


class TestEachChildRunIsItsOwn:
    async def test_each_loop_iteration_gets_its_own_record(self, driver):
        child = S.make_orchestration(id="orch_f251l_child", entry_step_id="echo",
                                     steps=[_print("echo", "saw {state.user_input}", key="seen")])
        parent = S.make_orchestration(id="orch_f251l", entry_step_id="lp", steps=[
            {"id": "lp", "name": "Each", "type": "loop", "loop_over": "state.items",
             "loop_step_ids": ["dive"], "output_key": "dives", "next_step_id": None},
            _call("agent_f251l", "dive", template="{state.loop_item}"),
        ])
        await S.seed_agents([S.make_orchestrator_agent(child["id"], id="agent_f251l")])
        await S.seed_orchestrations([parent, child])

        assert await driver.start(parent, f"run_f251l_{driver.kind}",
                                  initial_state={"items": ["Linear", "Jira", "Asana"]}) == "completed"

        ids = await _prefixed(f"run_f251l_{driver.kind}__dive_d1_")
        assert len(set(ids)) == 3
        runs = [await _run(i) for i in ids]
        assert [r.shared_state["user_input"] for r in runs] == ["Linear", "Jira", "Asana"]
        assert all(r.status == "completed" for r in runs)

    async def test_a_replay_runs_a_new_child_and_keeps_the_old_one(self):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        broken = {"id": "boom", "name": "Boom", "type": "transform", "transform_code": "raise ValueError('no')",
                  "output_key": "x", "next_step_id": None}
        child = S.make_orchestration(id="orch_f251r_child", entry_step_id="boom", steps=[broken])
        parent = S.make_orchestration(id="orch_f251r", entry_step_id="dive", steps=[_call("agent_f251r", "dive")])
        await S.seed_agents([S.make_orchestrator_agent(child["id"], id="agent_f251r")])
        await S.seed_orchestrations([parent, child])
        engine = OrchestrationEngine(Orchestration.model_validate(parent), _server())
        [e async for e in engine.run("go", run_id="run_f251r")]
        first = await _prefixed("run_f251r__dive_d1_")

        await S.seed_orchestrations([parent, S.make_orchestration(
            id="orch_f251r_child", entry_step_id="ok", steps=[_print("ok", "fine", key="fine")])])
        [e async for e in OrchestrationEngine.resume_failed("run_f251r", _server())]

        both = await _prefixed("run_f251r__dive_d1_")
        assert len(both) == 2 and first[0] in both
        assert (await _run(first[0])).status == "failed"
        assert (await _run("run_f251r")).status == "completed"


class TestAChildsEventsNameItsOwnStep:
    async def _three(self, l3_steps, l3_entry, **l3_kw):
        l3 = S.make_orchestration(id="orch_f251e_l3", name="L3", entry_step_id=l3_entry, steps=l3_steps, **l3_kw)
        l2 = S.make_orchestration(id="orch_f251e_l2", name="L2", entry_step_id="call2",
                                  steps=[_call("agent_f251e_l3", "call2", name="Pricing (L2)")])
        l1 = S.make_orchestration(id="orch_f251e", name="L1", entry_step_id="call1",
                                  steps=[_call("agent_f251e_l2", "call1", name="Market analysis")])
        await S.seed_agents([S.make_orchestrator_agent(l3["id"], id="agent_f251e_l3"),
                             S.make_orchestrator_agent(l2["id"], id="agent_f251e_l2")])
        await S.seed_orchestrations([l1, l2, l3])
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        return OrchestrationEngine(Orchestration.model_validate(l1), _server())

    async def test_a_grandchilds_step_is_named_down_the_chain(self):
        engine = await self._three([_print("research", "facts", key="facts")], "research")

        events = [e async for e in engine.run("go", run_id="run_f251e")]

        starts = [e for e in events if e.get("type") == "step_start" and e.get("nested_run_id")]
        deepest = next(e for e in starts if e.get("nested_step_id") == "research")
        assert deepest["step_name"] == "Market analysis"          # still routed as the parent's step
        assert deepest["nested_step_name"] == "Pricing (L2) › RESEARCH"
        completes = [e for e in events if e.get("type") == "step_complete"]
        # Only the parent's own step completes the parent's step.
        assert [e["orch_step_id"] for e in completes if not e.get("nested_run_id")] == ["call1"]

    async def test_a_childs_own_failure_is_not_the_parents_stream_error(self):
        engine = await self._three([_print("a", nxt="b"), _print("b")], "a", max_total_turns=1)

        events = [e async for e in engine.run("go", run_id="run_f251e2")]

        assert not [e for e in events if e.get("type") == "orchestration_error" and e.get("nested_run_id")]
        errors = [e for e in events if e.get("type") == "step_error" and e.get("orch_step_id") == "call1"
                  and not e.get("nested_run_id")]
        assert errors and "turn limit" in errors[-1]["reason"]


class TestChildrenAreListedUnderTheirParent:
    async def test_the_children_route(self, client):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        child = S.make_orchestration(id="orch_f251c_child", name="Child", entry_step_id="echo",
                                     steps=[_print("echo", "x", key="x")])
        parent = S.make_orchestration(id="orch_f251c", entry_step_id="a", steps=[
            _call("agent_f251c", "a", nxt="b"), _call("agent_f251c", "b")])
        await S.seed_agents([S.make_orchestrator_agent(child["id"], id="agent_f251c")])
        await S.seed_orchestrations([parent, child])
        engine = OrchestrationEngine(Orchestration.model_validate(parent), _server())
        [e async for e in engine.run("go", run_id="run_f251c")]

        resp = await client.get("/api/orchestrations/runs/run_f251c/children")

        assert resp.status_code == 200
        rows = resp.json()
        assert [r["run_id"].split("__")[1].split("_d1_")[0] for r in rows] == ["a", "b"]
        assert all(r["status"] == "completed" and r["orchestration_id"] == "orch_f251c_child" for r in rows)
        listed = [x["run_id"] for x in (await client.get("/api/orchestrations/runs")).json()]
        assert "run_f251c" in listed and not any(r["run_id"] in listed for r in rows)

    async def test_a_child_is_not_replayed_on_its_own(self, client):
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.state import SharedState
        await SharedState(OrchestrationRun(run_id="run_f251x__a_d1_abc123", orchestration_id="o",
                                           status="failed", current_step_id="s")).checkpoint()

        resp = await client.post("/api/orchestrations/runs/run_f251x__a_d1_abc123/resume")

        assert resp.status_code == 409
        assert "nested" in resp.json()["detail"]


async def _spend(run_id, cost, tokens=10):
    from datetime import datetime, timezone
    from core.store import usage as usage_store
    await usage_store.append({
        "timestamp": datetime.now(timezone.utc), "model": "m", "provider": "p", "session_id": "s",
        "agent_id": "a", "source": "orchestration", "run_id": run_id, "tool_name": None,
        "input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens, "context_chars": 0,
        "estimated_cost": cost, "latency_seconds": 0.0, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "estimated_savings": 0.0, "response_cache_hit": False,
    })


class TestARunsCostIncludesItsChildren:
    async def test_the_usage_page_folds_children_into_the_root(self):
        from core.usage_tracker import get_usage_summary
        await _spend("run_f251u_1_abc", 0.25)
        await _spend("run_f251u_1_abc__dive_d1_aaaaaa", 0.5)
        await _spend("run_f251u_1_abc__dive_d1_aaaaaa__x_d2_bbbbbb", 0.125)

        rows = (await get_usage_summary())["by_session"]
        mine = [r for r in rows if (r.get("run_id") or "").startswith("run_f251u_1")]
        assert len(mine) == 1 and mine[0]["run_id"] == "run_f251u_1_abc"
        assert mine[0]["estimated_cost"] == pytest.approx(0.875)

    async def test_a_runs_usage_log_includes_its_children_and_nothing_else(self):
        from core.usage_tracker import get_usage_logs
        await _spend("run_f251v_abc", 0.1)
        await _spend("run_f251v_abc__dive_d1_cccccc", 0.2)
        await _spend("run_f251v_abcXY", 0.4)       # another run whose id merely starts the same

        rows = await get_usage_logs(run_id="run_f251v_abc")

        assert sorted(r["run_id"] for r in rows) == ["run_f251v_abc", "run_f251v_abc__dive_d1_cccccc"]

    async def test_an_in_process_run_records_its_cost(self):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        orch = S.make_orchestration(id="orch_f251t", entry_step_id="only", steps=[_print("only", key="k")])
        await S.seed_orchestrations([orch])
        await _spend("run_f251t", 0.5, tokens=30)
        await _spend("run_f251t__earlier_d1_dddddd", 0.25, tokens=12)
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())

        [e async for e in engine.run("go", run_id="run_f251t")]

        run = await _run("run_f251t")
        assert run.total_cost_usd == pytest.approx(0.75) and run.total_tokens_used == 42


class TestTheRunListIsAPageOfRuns:
    async def test_many_children_do_not_crowd_out_runs(self):
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.state import SharedState
        for i in range(3):
            await SharedState(OrchestrationRun(run_id=f"run_f251p_{i}", orchestration_id="o_p",
                                               status="completed")).checkpoint()
            for j in range(25):
                await SharedState(OrchestrationRun(run_id=f"run_f251p_{i}__s_d1_{j:06x}", orchestration_id="o_c",
                                                   status="completed")).checkpoint()

        listed = await SharedState.list_runs(limit=3)

        assert sorted(r["run_id"] for r in listed) == ["run_f251p_0", "run_f251p_1", "run_f251p_2"]
