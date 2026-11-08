"""A Team agent in a workflow Agent step works as a team, inside the step's limits (TEAM1e, D290; 20 Q2, Q8, Q9).

Until TEAM1e a Team agent in a step ran one at a time (`delegate_to_agent`). Now a step whose agent's members work
"as a team" gives its lead `assign`, `message`, `wait` and `forward`, as chat does, and:
- the team is the step's own: two Team steps side by side never share one (its key is the run and the step);
- the step's time is the team's (Q2): the lead is told what is left, and near the end no more work is handed out;
- the run's cap is checked before every model call and every task (Q8): dollars as the run's spend plus this step's,
  credits as 1 + tasks;
- the step's one entry counts each member's task once (TEAM1d's price, 1 + tasks), a resumed member's again never;
- a delegate without the block, or one at a time, is unchanged (D204).
"""
import asyncio

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from _fakes.routed_llm import RoutedLLM, install

LEAD, ANA, BEN = "lead_t1e", "ana_t1e", "ben_t1e"


def _server():
    import types
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


@pytest.fixture
def llm(monkeypatch):
    return install(monkeypatch, RoutedLLM())


async def _seed(**lead):
    await S.seed_agents([
        S.make_agent(id=ANA, name="Ana", tools=[], skip_default_tools=True, max_turns=4),
        S.make_agent(id=BEN, name="Ben", tools=[], skip_default_tools=True, max_turns=4),
        S.make_agent(**{"id": LEAD, "name": "Lead", "type": "delegate", "tools": [], "skip_default_tools": True,
                        "delegate_agent_ids": [ANA, BEN], "team": {"mode": "team"}, **lead}),
    ])


def _step(sid="s1", prompt="PLAN IT", agent_id=LEAD, **kw):
    return {"id": sid, "name": f"Step {sid}", "type": "agent", "agent_id": agent_id, "prompt_template": prompt,
            "max_turns": 8, "next_step_id": None, **kw}


def _one_step(oid, **kw):
    return S.make_orchestration(id=oid, entry_step_id="s1", steps=[_step(**kw)])


async def _run(orch, run_id, message="go"):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    return [ev async for ev in engine.run(message, run_id=run_id)]


async def _entries(run_id):
    from core.orchestration.state import SharedState

    run = (await SharedState.restore(run_id)).run
    return run, [e for e in run.step_history if isinstance(e, dict)]


def _of(events, kind):
    return [e for e in events if e.get("type") == kind]


def _tools_of(call):
    return {t["function"]["name"] for t in (call.get("tools") or [])}


def assign(member, task):
    return tool_call("assign", member=member, task=task)


def both(*calls):
    return "\n".join(calls)


def leads(llm, scripts: dict):
    """A step's prompt opens with the run's sections and ends with its task: each lead's script by its task."""
    scripts = {k: list(v) for k, v in scripts.items()}

    async def route(kw):
        prompt = str(kw.get("prompt_msg") or "")
        for task, replies in scripts.items():
            if f"## TASK\n{task}" in prompt:
                reply = replies.pop(0) if len(replies) > 1 else replies[0]
                if callable(reply):
                    reply = await reply(kw)
                return reply
        return "Task complete."

    llm.otherwise([route])


def lead_calls(llm, task):
    return [c for c in llm.calls if c.get("_matched") is None and f"## TASK\n{task}" in str(c.get("prompt_msg") or "")]


class TestATeamStep:
    async def test_its_lead_assigns_and_its_members_work_side_by_side(self, llm):
        await _seed()
        met = asyncio.Barrier(2)

        async def meet(_kw):
            await asyncio.wait_for(met.wait(), timeout=5)
            return "found"

        leads(llm, {"PLAN IT": [both(assign("Ana", "CHECK A"), assign("Ben", "CHECK B")), tool_call("wait", seconds=10),
                           tool_call("wait", seconds=10), "both checked"]})
        llm.on("CHECK A", [meet]).on("CHECK B", [meet])
        events = await _run(_one_step("orch_t1e_a"), "run_t1e_a")
        assert "assign" in _tools_of(lead_calls(llm, "PLAN IT")[0])
        assert "delegate_to_agent" not in _tools_of(lead_calls(llm, "PLAN IT")[0])
        started = _of(events, "member_started")
        assert sorted(e["member_name"] for e in started) == ["Ana", "Ben"]
        assert all(e.get("orch_step_id") == "s1" for e in started)
        run, entries = await _entries("run_t1e_a")
        assert run.status == "completed"
        # One entry, its price 1 + 2 tasks (TEAM1d's), and the team's own list kept off the run's (F536).
        assert [(e["step_id"], e["status"], e.get("tasks")) for e in entries] == [("s1", "completed", 2)]
        assert not run.tasks

    async def test_a_delegate_one_at_a_time_is_unchanged(self, llm):
        await _seed(team={"mode": "one_at_a_time"})
        leads(llm, {"PLAN IT": [tool_call("delegate_to_agent", agent_id=ANA, task="CHECK A"), "done"]})
        llm.on("CHECK A", ["A fine"])
        events = await _run(_one_step("orch_t1e_b"), "run_t1e_b")
        assert "delegate_to_agent" in _tools_of(lead_calls(llm, "PLAN IT")[0])
        assert not _of(events, "member_started")
        _, entries = await _entries("run_t1e_b")
        assert [e.get("tasks") for e in entries] == [1]

    async def test_two_team_steps_at_once_in_one_conversation_keep_their_own_teams(self, llm):
        """Two runs started from one chat share its session id; each step's team is its run's and step's own, so a
        person's Stop or answer reaches the right one (a Parallel runs a delegate's branch in order already)."""
        from core import team as team_mod
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        await _seed()
        seen: list[set] = []
        met = asyncio.Barrier(2)

        async def look(_kw):
            await asyncio.wait_for(met.wait(), timeout=5)
            seen.append({k[1] for k in team_mod._LIVE if k[1].startswith("run:run_t1e_c")})
            return assign("Ana", "CHECK")

        leads(llm, {"PLAN IT": [look, look, tool_call("wait", seconds=10), "done"]})
        llm.on("CHECK", ["checked"])
        orch = Orchestration.model_validate(_one_step("orch_t1e_c"))

        async def one(run_id):
            engine = OrchestrationEngine(orch, _server())
            return [ev async for ev in engine.run("go", run_id=run_id, session_id="s_shared_t1e")]

        await asyncio.gather(one("run_t1e_c1"), one("run_t1e_c2"))
        assert seen and all(keys == {"run:run_t1e_c1/s1", "run:run_t1e_c2/s1"} for keys in seen)
        for run_id in ("run_t1e_c1", "run_t1e_c2"):
            _, entries = await _entries(run_id)
            assert [e.get("tasks") for e in entries] == [1]


class TestTheStepsTime:
    async def test_the_lead_is_told_the_time_left(self, llm):
        await _seed()
        leads(llm, {"PLAN IT": ["alone"]})
        await _run(_one_step("orch_t1e_d", timeout_seconds=600), "run_t1e_d")
        prompt = str(lead_calls(llm, "PLAN IT")[0].get("prompt_msg") or "")
        assert "left for this step" in prompt

    async def test_near_the_end_no_more_work_is_handed_out(self, llm, monkeypatch):
        from core import team as team_mod

        await _seed()
        # The whole of a 60 s step is "near the end" here.
        monkeypatch.setattr(team_mod, "TIME_GUARD_MIN_S", 120)
        leads(llm, {"PLAN IT": [assign("Ana", "CHECK A"), "finished with what I have"]})
        events = await _run(_one_step("orch_t1e_e", timeout_seconds=60), "run_t1e_e")
        assert not _of(events, "member_started")
        refused = [e for e in _of(events, "tool_result") if e.get("tool_name") == "assign"]
        assert refused and "time is nearly up" in str(refused[0].get("preview") or refused[0].get("result"))
        _, entries = await _entries("run_t1e_e")
        assert entries[0]["status"] == "completed" and "tasks" not in entries[0]


class TestTheRunsCap:
    async def test_its_dollar_cap_stops_the_team_in_its_own_words(self, llm, monkeypatch):
        from core import team as team_mod
        from core.orchestration import run_cap

        await _seed()

        async def reader(run_id, step_type, cap):
            return run_cap.Spend(usd=0.0095)

        monkeypatch.setattr(run_cap, "_reader", reader)
        monkeypatch.setattr(team_mod.Team, "allowance", lambda self, *a, **k: 0.001)
        leads(llm, {"PLAN IT": [assign("Ana", "CHECK A"), "never"]})
        events = await _run(_one_step("orch_t1e_f") | {"max_run_usd": 0.01}, "run_t1e_f")
        assert not lead_calls(llm, "PLAN IT")
        # CAP1's own words: the step stops there, as the run would have between steps.
        errors = [e for e in _of(events, "step_error") if e.get("orch_step_id") == "s1"]
        assert errors and errors[0]["reason"].startswith(
            "Stopped: this run reached its cap of $0.01 in model costs. It had spent about $0.0095.")

    async def test_its_credit_cap_refuses_the_task_past_it(self, llm, monkeypatch):
        from core.orchestration import run_cap

        await _seed()

        async def reader(run_id, step_type, cap):
            # 1 credit used, this step's 1 makes 2, so a cap of 3 leaves room for one task.
            return run_cap.Spend(credits=1.0, credits_after=2.0, step_credits=1.0)

        monkeypatch.setattr(run_cap, "_reader", reader)
        leads(llm, {"PLAN IT": [both(assign("Ana", "CHECK A"), assign("Ben", "CHECK B")), tool_call("wait", seconds=10),
                           "one checked"]})
        llm.on("CHECK A", ["A fine"]).on("CHECK B", ["B fine"])
        events = await _run(_one_step("orch_t1e_g") | {"max_run_credits": 3}, "run_t1e_g")
        assert [e["member_name"] for e in _of(events, "member_started")] == ["Ana"]
        refused = [e for e in _of(events, "tool_result") if e.get("tool_name") == "assign"
                   and "cap of 3 credits" in str(e.get("preview") or e.get("result"))]
        assert len(refused) == 1
        _, entries = await _entries("run_t1e_g")
        assert entries[0].get("tasks") == 1


class TestTheRunsLog:
    def test_a_teams_shape_survives_the_event_cap(self):
        from core.orchestration.journal import STRUCTURAL_EVENT_TYPES

        assert {"member_started", "member_finished", "team_tasks", "member_answered"} <= STRUCTURAL_EVENT_TYPES
        assert "member_event" not in STRUCTURAL_EVENT_TYPES
