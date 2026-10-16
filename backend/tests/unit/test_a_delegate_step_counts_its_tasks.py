"""A delegate's Agent step counts the member and helper tasks it ran, on its one history entry (TEAM1d, 20 Q7).

"Parallel saves time, not money": in a workflow a delegate's step keeps its one `agent` entry (the loop guard and
EM1's pause ordinals are untouched), and that entry carries `tasks`, the hand-offs and waiting helpers it ran, which
cloud prices 1 credit plus 1 per task. A hand-off is counted when its answer is back, so one a person approved midway
is counted once, after the resume; one refused before it ran (a member it cannot reach) is not counted. A plain
agent's step carries no count, helpers or not (Naveen, TEAM1d's plan: priced as before).
"""
import types

from _fakes import seed as S
from _fakes.fake_llm import tool_call

from core.orchestration.pauses import TASKS_KEY, end_step, record_pause, take_tasks

LEAD, MEMBER, PLAIN = "lead_t1d", "member_t1d", "plain_t1d"


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _run(orch, run_id):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    return [ev async for ev in engine.run("go", run_id=run_id)]


async def _entries(run_id):
    from core.orchestration.state import SharedState

    run = (await SharedState.restore(run_id)).run
    return run, [e for e in run.step_history if isinstance(e, dict)]


async def _seed():
    await S.seed_agents([
        S.make_agent(id=MEMBER, name="Member", tools=[]),
        S.make_agent(id=LEAD, name="Lead", type="delegate", tools=[], delegate_agent_ids=[MEMBER]),
        S.make_agent(id=PLAIN, name="Plain", tools=[]),
    ])


def _one_step(oid, agent_id):
    return S.make_orchestration(id=oid, entry_step_id="s1", steps=[
        {"id": "s1", "name": "Step", "type": "agent", "agent_id": agent_id, "max_turns": 6, "next_step_id": None}])


async def test_two_hand_offs_are_two_tasks_on_the_steps_one_entry(fake_llm):
    await _seed()
    fake_llm.script([
        tool_call("delegate_to_agent", agent_id=MEMBER, task="first"),
        "member: one",
        tool_call("delegate_to_agent", agent_id=MEMBER, task="second"),
        "member: two",
        "lead: done",
    ])
    await _run(_one_step("orch_t1d_a", LEAD), "run_t1d_a")
    run, entries = await _entries("run_t1d_a")
    assert [(e["step_id"], e["status"], e.get("tasks")) for e in entries] == [("s1", "completed", 2)]
    assert TASKS_KEY not in run.shared_state


async def test_a_hand_off_to_a_member_it_cannot_reach_is_not_a_task(fake_llm):
    await _seed()
    fake_llm.script([
        tool_call("delegate_to_agent", agent_id="nobody", task="first"),
        tool_call("delegate_to_agent", agent_id=MEMBER, task="second"),
        "member: two",
        "lead: done",
    ])
    await _run(_one_step("orch_t1d_b", LEAD), "run_t1d_b")
    _, entries = await _entries("run_t1d_b")
    assert [e.get("tasks") for e in entries] == [1]


async def test_a_plain_agents_step_carries_no_count(fake_llm):
    await _seed()
    fake_llm.script(["plain: done"])
    await _run(_one_step("orch_t1d_c", PLAIN), "run_t1d_c")
    _, entries = await _entries("run_t1d_c")
    assert len(entries) == 1 and "tasks" not in entries[0]


async def test_a_delegate_that_answers_alone_carries_no_count(fake_llm):
    await _seed()
    fake_llm.script(["lead: I know this one"])
    await _run(_one_step("orch_t1d_d", LEAD), "run_t1d_d")
    _, entries = await _entries("run_t1d_d")
    assert len(entries) == 1 and "tasks" not in entries[0]


class TestTheEntry:
    def test_take_tasks_moves_the_count_off_the_run(self):
        state = {TASKS_KEY: {"s1": 3, "s2": 1}}
        assert take_tasks(state, "s1") == {"tasks": 3}
        assert state == {TASKS_KEY: {"s2": 1}}
        assert take_tasks(state, "s2") == {"tasks": 1} and TASKS_KEY not in state
        assert take_tasks(state, "s3") == {}

    def test_a_reopened_entry_adds_what_the_resumed_stretch_ran(self):
        history = [{"step_id": "s1", "step_type": "agent", "status": "running", "tasks": 2}]
        end_step(history, {"step_id": "s1", "step_type": "agent", "status": "completed", "tasks": 1})
        assert [(e["status"], e.get("tasks")) for e in history] == [("completed", 3)]

    def test_a_reopened_entry_keeps_its_count_when_the_resumed_stretch_ran_none(self):
        history = [{"step_id": "s1", "step_type": "agent", "status": "running", "tasks": 2}]
        end_step(history, {"step_id": "s1", "step_type": "agent", "status": "completed"})
        assert history[0]["tasks"] == 2

    def test_a_second_pause_carries_the_count_to_the_new_entry(self):
        history = [{"step_id": "s1", "step_type": "agent", "status": "running", "tasks": 2}]
        record_pause(history, {"step_id": "s1", "step_type": "agent", "status": "paused", "tasks": 1})
        assert [(e["step_type"], e.get("tasks")) for e in history] == [("human", None), ("agent", 3)]


DANGER = {"type": "function", "function": {"name": "danger", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}}}
ASKER = "asker_t1d"


async def test_a_hand_off_a_person_approved_midway_is_counted_once(fake_llm):
    """The member asks before its call; the step pauses with the first hand-off counted, and the resumed one adds the
    second when its answer is back (F286's replay, never counted twice)."""
    import unittest.mock as _m

    import core.react_engine as re_mod
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    await S.seed_agents([
        S.make_agent(id=MEMBER, name="Member", tools=[]),
        S.make_agent(id=ASKER, name="Asker", tools=["all"], skip_default_tools=True,
                     tool_policy={"undeclared": "confirm"}),
        S.make_agent(id=LEAD, name="Lead", type="delegate", tools=[], delegate_agent_ids=[MEMBER, ASKER]),
    ])
    real = re_mod.run_agent_step

    async def wrapped(**kwargs):
        if kwargs.get("agent_id") == ASKER:
            async def executor(name, args):
                return f"ran-{name}"
            kwargs.setdefault("tools_override", [DANGER])
            kwargs.setdefault("tool_executor", executor)
        async for ev in real(**kwargs):
            yield ev

    orch = _one_step("orch_t1d_e", LEAD)
    await S.seed_orchestrations([orch])
    with _m.patch.object(re_mod, "run_agent_step", wrapped):
        fake_llm.script([
            tool_call("delegate_to_agent", agent_id=MEMBER, task="first"),
            "member: one",
            tool_call("delegate_to_agent", agent_id=ASKER, task="second"),
            tool_call("danger"),
        ])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        first = [e async for e in engine.run("go", run_id="run_t1d_e")]
        assert first[-1]["type"] == "human_input_required"
        _, paused = await _entries("run_t1d_e")
        assert [(e["status"], e.get("tasks")) for e in paused] == [("paused", 1)]

        fake_llm.script(["asker: done", "lead: all done"])
        [e async for e in OrchestrationEngine.resume("run_t1d_e", {"approve": True}, _server())]
    run, entries = await _entries("run_t1d_e")
    assert run.status == "completed"
    assert [(e["step_type"], e["status"], e.get("tasks")) for e in entries] == [("agent", "completed", 2)]
