"""A delegate's task list never replaces its workflow run's (F536).

TEAM0 reproduced it (`MEMORY/drafts/team0/f536/`): a one-step workflow whose Agent step's agent is a delegate. The
lead wrote a two-task plan and delegated; the delegate wrote its own one-task plan; the run ended `completed` holding
the delegate's. The engine re-yields every non-final event a delegate sends, tagged `delegate_from`, and the step
took any `tasks_updated` as the run's plan and checkpointed it. So the run page showed the delegate's plan, the next
step was seeded with it, and a crash-resume handed the lead the delegate's open tasks. Beside other Parallel branches
it was worse: the lead's own tasks were counted as dropped and removed from the run.

The run's plan is its own agents'. A delegate's events still stream and are still journalled — they are its history —
but its plan is not the run's. Cloud chat's `reduceDelegated` and `session.py`'s readers skip them the same way.
"""
import asyncio
import types

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core.task_list import TASK_TOOL_NAME

LEAD, HELPER = "lead_f536", "helper_f536"
LEAD_PLAN = [{"content": "lead: split the work", "status": "completed"},
             {"content": "lead: write the summary", "status": "completed"}]
HELPER_PLAN = [{"content": "helper: look things up", "status": "completed"}]


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _run(orch, run_id):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    return [ev async for ev in engine.run("go", run_id=run_id)]


async def _tasks(run_id):
    import core.orchestration.state as state_mod

    return [t["content"] for t in (await state_mod.get_state_backend().load(run_id)).tasks]


async def _seed():
    await S.seed_agents([
        S.make_agent(id=HELPER, name="Helper", tools=[]),
        S.make_agent(id=LEAD, name="Lead", type="delegate", tools=[], delegate_agent_ids=[HELPER]),
        S.make_agent(id="next_f536", name="Next", tools=[]),
        S.make_agent(id="plain_lead_f536", name="Plain lead", tools=[]),
    ])


async def test_the_run_keeps_the_leads_plan(fake_llm):
    await _seed()
    orch = S.make_orchestration(id="orch_f536", entry_step_id="s1", steps=[
        {"id": "s1", "name": "Lead", "type": "agent", "agent_id": LEAD, "next_step_id": None}])
    fake_llm.script([
        tool_call(TASK_TOOL_NAME, tasks=LEAD_PLAN),
        tool_call("delegate_to_agent", agent_id=HELPER, task="look things up"),
        tool_call(TASK_TOOL_NAME, tasks=HELPER_PLAN),
        "helper: found it",
        "lead: done",
    ])
    events = await _run(orch, "run_f536")
    # The delegate's plan still streams, tagged as its own (its history; the run log shows it as the delegate's).
    assert [e["delegate_from"] for e in events if e.get("type") == "tasks_updated" and e.get("delegate_from")]
    assert await _tasks("run_f536") == [t["content"] for t in LEAD_PLAN]


async def test_the_next_step_is_seeded_with_neither_plan(fake_llm):
    """F536 kept the delegate's plan out of the next step; F1249 keeps the lead's out too: a step starts with its own
    list, and the step after the lead is not the lead (it held the lead's open task as its own; Naveen's write_posts,
    10-29)."""
    await _seed()
    orch = S.make_orchestration(id="orch_f536b", entry_step_id="s1", steps=[
        {"id": "s1", "name": "Lead", "type": "agent", "agent_id": LEAD, "next_step_id": "s2"},
        {"id": "s2", "name": "Next", "type": "agent", "agent_id": "next_f536", "next_step_id": None}])
    fake_llm.script([
        tool_call(TASK_TOOL_NAME, tasks=[{"content": "lead: split the work", "status": "completed"},
                                         {"content": "lead: write the summary", "status": "pending"}]),
        tool_call("delegate_to_agent", agent_id=HELPER, task="look things up"),
        tool_call(TASK_TOOL_NAME, tasks=[{"content": "helper: look things up", "status": "pending"}]),
        "helper: found it",
        "lead: handing over",
        "next: done",
    ])
    await _run(orch, "run_f536b")
    told = fake_llm.calls[-1]["prompt_msg"]
    assert "lead: write the summary" not in told
    assert "helper: look things up" not in told
    assert await _tasks("run_f536b") == ["lead: split the work", "lead: write the summary"], "the run's view keeps it"


async def test_beside_other_branches_the_leads_tasks_stay(monkeypatch):
    """The F255 merge: a branch's rewrite replaces its own tasks in the run's list. A delegate's plan is not the
    lead's rewrite, so it neither lands nor makes the lead's tasks look dropped.

    Plain agents, so the two branches run together (a delegate-type branch can ask, and runs after the others);
    the step's stream carries a delegate's tagged plan as the engine re-yields one."""
    import core.react_engine as re_mod

    async def fake_step(**kwargs):
        # Branches start together and then write, as a model call's wait makes them (F255's own test sleeps too).
        await asyncio.sleep(0.05)
        if kwargs["agent_id"] == "plain_lead_f536":
            yield {"type": "tasks_updated", "tasks": LEAD_PLAN}
            yield {"type": "tasks_updated", "tasks": HELPER_PLAN, "delegate_from": LEAD,
                   "delegate_agent_name": "Helper"}
            yield {"type": "tasks_incomplete", "tasks": HELPER_PLAN, "delegate_from": LEAD}
            yield {"type": "final", "response": "lead: done"}
        else:
            yield {"type": "tasks_updated", "tasks": [{"content": "solo: check prices", "status": "completed"}]}
            yield {"type": "final", "response": "solo: done"}

    monkeypatch.setattr(re_mod, "run_agent_step", fake_step)
    await _seed()
    orch = S.make_orchestration(id="orch_f536c", entry_step_id="par", steps=[
        {"id": "par", "name": "Both", "type": "parallel", "parallel_branches": [["a"], ["b"]], "next_step_id": None},
        {"id": "a", "name": "Lead", "type": "agent", "agent_id": "plain_lead_f536"},
        {"id": "b", "name": "Solo", "type": "agent", "agent_id": "next_f536"}])
    events = await _run(orch, "run_f536c")
    assert any(e.get("type") == "orchestration_complete" for e in events), [e.get("type") for e in events][-5:]
    assert sorted(await _tasks("run_f536c")) == sorted(["lead: split the work", "lead: write the summary",
                                                        "solo: check prices"])
