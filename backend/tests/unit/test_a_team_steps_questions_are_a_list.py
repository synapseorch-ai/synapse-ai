"""A Team step's questions are a list, each answered by its own id (TEAM1e, D290; TEAM1c's settled shape, 20 Q6).

Two members of a Team agent in a workflow step ask a person at once: the step pauses once, on both questions (one
paused entry, one ordinal), and its pending approval lists them, each `{member_id}:{approval_id}`. An answer names
its question: that member carries on, the other stays asked, and when the step pauses again on it, that is a new
pause with its own ordinal (F463), so a stale answer never answers a later one. An answer to another question of the
run, given while it is paused or working, is held for the run's team and taken once. The run's view and its bell list
every question.
"""
import unittest.mock as _m

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from _fakes.routed_llm import RoutedLLM, install
from test_human_in_containers import worker  # noqa: F401 — the fleet's job, its pool and settings stubbed

LEAD, ANA, BEN = "lead_t1eq", "ana_t1eq", "ben_t1eq"
DANGER = {"type": "function", "function": {"name": "danger", "description": "changes a thing",
                                           "parameters": {"type": "object", "properties": {"item": {"type": "string"}}}}}


def _server():
    import types
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


@pytest.fixture
def llm(monkeypatch):
    return install(monkeypatch, RoutedLLM())


@pytest.fixture
def ran(monkeypatch):
    """The members' gated tool, run by the test: what ran, in order."""
    import core.react_engine as re_mod

    calls: list[tuple[str, dict]] = []
    real = re_mod.run_agent_step

    async def executor(name, args):
        calls.append((name, dict(args)))
        return f"{name} done for {args.get('item')}"

    async def wrapped(**kwargs):
        if kwargs.get("agent_id") in (ANA, BEN):
            kwargs.setdefault("tools_override", [DANGER])
            kwargs.setdefault("tool_executor", executor)
        async for ev in real(**kwargs):
            yield ev

    monkeypatch.setattr(re_mod, "run_agent_step", wrapped)
    return calls


async def _seed():
    asks = {"tools": ["all"], "skip_default_tools": True, "tool_policy": {"undeclared": "confirm"}, "max_turns": 4}
    await S.seed_agents([
        S.make_agent(id=ANA, name="Ana", **asks),
        S.make_agent(id=BEN, name="Ben", **asks),
        S.make_agent(id=LEAD, name="Lead", type="delegate", tools=[], skip_default_tools=True,
                     delegate_agent_ids=[ANA, BEN], team={"mode": "team"}),
    ])


def _orch(oid):
    return S.make_orchestration(id=oid, entry_step_id="s1", steps=[
        {"id": "s1", "name": "Change", "type": "agent", "agent_id": LEAD, "prompt_template": "PLAN IT",
         "max_turns": 12, "next_step_id": None}])


def leads(llm, replies):
    """The step's prompt ends with its task; the members' start `User Request: CHANGE …`."""
    llm.otherwise(list(replies))


def assign(member, task):
    return tool_call("assign", member=member, task=task)


def _waits(n):
    # Each its own seconds: three identical waits would be ended by the repeat guard (F476).
    return [tool_call("wait", seconds=10 + i) for i in range(n)]


async def _run_and_pause(orch, run_id):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    return [ev async for ev in engine.run("go", run_id=run_id)]


async def _resume(run_id, response):
    from core.orchestration.engine import OrchestrationEngine

    return [ev async for ev in OrchestrationEngine.resume(run_id, response, _server())]


async def _run(run_id):
    from core.orchestration.state import SharedState

    return (await SharedState.restore(run_id)).run


def _pending(run):
    return (run.step_progress.get("s1") or {}).get("pending_approval") or {}


def _scripts(llm):
    leads(llm, ["\n".join([assign("Ana", "CHANGE A"), assign("Ben", "CHANGE B")]), *_waits(8), "both changed"])
    llm.on("CHANGE A", [tool_call("danger", item="a"), "A changed"])
    llm.on("CHANGE B", [tool_call("danger", item="b"), "B changed"])


class TestTwoQuestionsInOneStep:
    async def test_the_step_pauses_once_on_both_and_each_is_answered_by_its_own_id(self, llm, ran):
        from core.orchestration.pauses import pause_ordinal
        from core.orchestration.run_view import read_run

        await _seed()
        _scripts(llm)
        events = await _run_and_pause(_orch("orch_t1eq_a"), "run_t1eq_a")
        asked = [e for e in events if e.get("type") == "human_input_required"][-1]
        ids = {q["member_name"]: q["question_id"] for q in asked["questions"]}
        assert set(ids) == {"Ana", "Ben"} and not ran

        run = await _run("run_t1eq_a")
        assert run.status == "paused"
        assert [q["question_id"] for q in _pending(run)["questions"]] == list(asked["questions"][i]["question_id"]
                                                                          for i in range(2))
        assert len(run.step_progress["s1"]["team_pending"]) == 2
        assert [e["status"] for e in run.step_history] == ["paused"]
        first_ordinal = pause_ordinal(run.step_history)
        view = await read_run("run_t1eq_a")
        assert {q["member_name"] for q in view["human_input_required"]["questions"]} == {"Ana", "Ben"}

        # Ben's answered first: only Ben's call runs; Ana's is asked again, in a pause of its own.
        events = await _resume("run_t1eq_a", {"approve": True, "question_id": ids["Ben"]})
        assert ran == [("danger", {"item": "b"})]
        run = await _run("run_t1eq_a")
        assert run.status == "paused"
        assert [q["member_name"] for q in _pending(run)["questions"]] == ["Ana"]
        assert pause_ordinal(run.step_history) != first_ordinal

        # A stale answer (Ben's again) is not Ana's.
        from core.orchestration.answers import QuestionNotAsked, check_question
        with pytest.raises(QuestionNotAsked):
            check_question(run, ids["Ben"])

        await _resume("run_t1eq_a", {"approve": False, "reason": "not today", "question_id": ids["Ana"]})
        assert ran == [("danger", {"item": "b"})]   # Ana's was refused, never run
        run = await _run("run_t1eq_a")
        assert run.status == "completed"
        agent_entries = [e for e in run.step_history if e.get("step_type") == "agent"]
        assert [e["status"] for e in agent_entries] == ["completed"]
        assert agent_entries[0].get("tasks") == 2

    async def test_an_answer_held_while_the_run_waits_is_taken_with_the_resume(self, llm, ran):
        from core.chat_approval import post_answer
        from core.team import run_mailbox

        await _seed()
        _scripts(llm)
        events = await _run_and_pause(_orch("orch_t1eq_b"), "run_t1eq_b")
        ids = {q["member_name"]: q["question_id"]
               for q in [e for e in events if e.get("type") == "human_input_required"][-1]["questions"]}
        # Ana's answer arrives at another door while the run is still paused on both (a second approver).
        assert post_answer(run_mailbox("run_t1eq_b"), "run", {"question_id": ids["Ana"], "approve": True}) == "held"
        await _resume("run_t1eq_b", {"approve": True, "question_id": ids["Ben"]})
        assert sorted(args["item"] for _, args in ran) == ["a", "b"]
        run = await _run("run_t1eq_b")
        assert run.status == "completed"

    async def test_an_answer_with_no_question_answers_the_first_as_before(self, llm, ran):
        await _seed()
        _scripts(llm)
        events = await _run_and_pause(_orch("orch_t1eq_c"), "run_t1eq_c")
        first = [e for e in events if e.get("type") == "human_input_required"][-1]["questions"][0]
        await _resume("run_t1eq_c", {"approve": True})
        assert len(ran) == 1 and ran[0][1]["item"] == ("a" if first["member_name"] == "Ana" else "b")


class TestTheRunsBell:
    async def test_each_question_is_its_own_item_and_settles_by_its_id(self, llm, ran):
        from core.notifications import hub

        await _seed()
        _scripts(llm)
        with _m.patch.object(hub, "publish", wraps=hub.publish) as published:
            events = await _run_and_pause(_orch("orch_t1eq_d"), "run_t1eq_d")
            for ev in events:
                await hub.observe_run_event("run_t1eq_d", ev)
        items = [c.kwargs.get("data") or c.args[4] if len(c.args) > 4 else c.kwargs.get("data")
                 for c in published.call_args_list if (c.args and c.args[0] == "human_input")]
        qids = sorted(d.get("question_id") for d in items)
        asked = [e for e in events if e.get("type") == "human_input_required"][-1]["questions"]
        assert qids == sorted(q["question_id"] for q in asked)


class TestTheDoors:
    """`answer_paused_run` — behind the run page, v1 and v2 — names a question, and reaches a working team."""

    async def test_an_answer_while_the_step_works_reaches_its_member_without_a_resume(self, llm, ran):
        import asyncio
        import types

        from core.orchestration.answers import answer_paused_run
        from core.orchestration.runner import SENTINEL

        await _seed()
        go_on = asyncio.Event()

        async def ben_waits(_kw):
            await asyncio.wait_for(go_on.wait(), timeout=5)
            return "B changed"

        leads(llm, ["\n".join([assign("Ana", "CHANGE A"), assign("Ben", "CHANGE B")]), *_waits(8), "both changed"])
        llm.on("CHANGE A", [tool_call("danger", item="a"), "A changed"])
        llm.on("CHANGE B", [tool_call("danger", item="b"), ben_waits])
        events = await _run_and_pause(_orch("orch_t1eq_e"), "run_t1eq_e")
        ids = {q["member_name"]: q["question_id"]
               for q in [e for e in events if e.get("type") == "human_input_required"][-1]["questions"]}
        app = types.SimpleNamespace(server_module=_server())

        resumed = await answer_paused_run(app, "run_t1eq_e", {"approve": True}, question_id=ids["Ben"])
        assert resumed.queue is not None and resumed.live is None
        for _ in range(200):   # Ben's call has run, and Ben works on
            if ran:
                break
            await asyncio.sleep(0.01)
        live = await answer_paused_run(app, "run_t1eq_e", {"approve": True}, question_id=ids["Ana"])
        assert live.live == "applied" and live.queue is None
        go_on.set()
        while (await asyncio.wait_for(resumed.queue.get(), timeout=10)) is not SENTINEL:
            pass
        assert sorted(args["item"] for _, args in ran) == ["a", "b"]
        run = await _run("run_t1eq_e")
        assert run.status == "completed"
        assert len([e for e in run.step_history if e.get("status") == "paused"]) == 0

    async def test_a_question_the_paused_run_does_not_ask_is_refused_before_anything_runs(self, llm, ran):
        import types

        from core.orchestration.answers import QuestionNotAsked, answer_paused_run

        await _seed()
        _scripts(llm)
        await _run_and_pause(_orch("orch_t1eq_f"), "run_t1eq_f")
        with pytest.raises(QuestionNotAsked):
            await answer_paused_run(types.SimpleNamespace(server_module=_server()), "run_t1eq_f",
                                    {"approve": True}, question_id="m_nobody:ap_x")
        assert (await _run("run_t1eq_f")).status == "paused" and not ran


class TestTheLinesOnTheRunPage:
    async def test_a_members_stop_from_the_run_reaches_its_team(self, llm):
        import asyncio
        import types

        from core.routes.orchestrations import _ask_runs_team

        await S.seed_agents([
            S.make_agent(id=ANA, name="Ana", tools=[], skip_default_tools=True, max_turns=4),
            S.make_agent(id=BEN, name="Ben", tools=[], skip_default_tools=True, max_turns=4),
            S.make_agent(id=LEAD, name="Lead", type="delegate", tools=[], skip_default_tools=True,
                         delegate_agent_ids=[ANA, BEN], team={"mode": "team"}),
        ])
        request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace()))
        stopped: list[bool] = []

        async def ana_works(_kw):
            from core import team as team_mod
            (working,) = team_mod.at(team_mod.run_mailbox("run_t1eq_g"))
            (ana,) = [m for m in working.members.values() if m.name == "Ana"]
            stopped.append(await _ask_runs_team(request, "run_t1eq_g", {"kind": "stop", "member_id": ana.id}))
            return tool_call("wait", seconds=1)   # a Stop is read before its next step

        leads(llm, [assign("Ana", "CHANGE A"), *_waits(3), "stopped Ana"])
        llm.on("CHANGE A", [ana_works, "never"])
        events = await _run_and_pause(_orch("orch_t1eq_g"), "run_t1eq_g")
        assert stopped == [True]
        (done,) = [e for e in events if e.get("type") == "member_finished"]
        assert done["status"] == "cancelled"
        assert await _ask_runs_team(request, "run_t1eq_g", {"kind": "lead", "text": "hi"}) is False


class TestOnTheFleet:
    async def test_two_people_answer_two_members_at_once_and_both_are_applied(self, llm, ran, fake_redis, worker,
                                                                              monkeypatch):
        """Both answer jobs read the pause before either changes it; one claims the run (F136), and the other's
        answer goes to the run's team, which takes it while its step works or before it pauses again."""
        import asyncio

        from test_answers_at_once import _together
        from test_human_in_containers import _ctx
        from core.orchestration.pauses import pause_ordinal

        await _seed()
        _scripts(llm)
        orch = _orch("orch_t1eq_h")
        await S.seed_orchestrations([orch])
        started = await worker.run_orchestration_job(
            await _ctx(fake_redis), run_id="run_t1eq_h", orch_id="orch_t1eq_h", initial_input="go")
        assert started["status"] == "paused"
        run = await _run("run_t1eq_h")
        ids = {q["member_name"]: q["question_id"] for q in _pending(run)["questions"]}
        asking = pause_ordinal(run.step_history)
        _together(monkeypatch, worker, "_load_orchestration")

        async def answer(name: str, job_id: str) -> dict:
            ctx = {**await _ctx(fake_redis), "job_id": job_id}
            return await worker.resume_orchestration_job(
                ctx, run_id="run_t1eq_h", human_response={"approve": True, "question_id": ids[name]},
                pause_ordinal=asking)

        results = await asyncio.gather(answer("Ana", "resume_a"), answer("Ben", "resume_b"))
        assert len([r for r in results if r.get("applied", True)]) == 1, results
        assert sorted(args["item"] for _, args in ran) == ["a", "b"]
        assert (await _run("run_t1eq_h")).status == "completed"


def test_a_runs_mailbox_is_its_top_runs():
    from core.orchestration.run_cap import root_of
    from core.team import run_mailbox

    for run_id in ("run_a", "run_a__child_1", "run_a__child_1__grand"):
        assert run_mailbox(run_id) == f"run:{root_of(run_id)}" == "run:run_a"
