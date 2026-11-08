"""Delegating to a workflow (orchestrator) agent runs its workflow (F262).

`delegate_to_agent` ran every target as a plain chat agent. An orchestrator
agent has no tools of its own and a one-line stub for a prompt, so the
"workflow" answered from the model's own knowledge after rummaging in the
default file tools — another chat's compaction archive included — and the lead
reported it as "exactly what the workflow agent returned".

Now an orchestrator target runs its orchestration, as an Agent step and a
schedule do: its result is the delegate's answer; a failure says so; a pause
for a person is not the lead's to answer, so the lead is told where it waits.
A delegation into a workflow already running further up the chain is refused.
"""
import types

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from tests.unit.test_a_failed_child_fails_its_parent import _human, _print  # noqa: F401


def _server():
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


async def _team(orch_id, steps, entry):
    orch = S.make_orchestration(id=orch_id, name="L3 Competitor Deep Dive", entry_step_id=entry, steps=steps)
    workflow = S.make_orchestrator_agent(orch_id, id=f"orch_agent_{orch_id}", name="Deep Dive")
    lead = S.make_agent(id=f"lead_{orch_id}", name="Tech Lead", type="delegate", tools=[],
                        delegate_agent_ids=[workflow["id"]])
    await S.seed_orchestrations([orch])
    await S.seed_agents([workflow, lead])
    return lead, workflow


async def _lead_turn(lead, message="get the deep dive"):
    from core.react_engine import run_agent_step
    return [e async for e in run_agent_step(message=message, agent_id=lead["id"], session_id="s_f262",
                                            server_module=_server(), max_turns=3)]


def _delegate_result(events):
    return next(e["preview"] for e in events if e.get("type") == "tool_result"
                and e.get("tool_name") == "delegate_to_agent")


async def _runs_of(orch_id):
    from core.orchestration.state import SharedState
    return [r for r in await SharedState.list_runs(limit=50) if r["orchestration_id"] == orch_id]


class TestItRunsTheWorkflow:
    async def test_the_workflow_runs_and_its_result_is_the_answer(self, fake_llm):
        lead, workflow = await _team("orch_f262", [_print("dive", "dove into {state.user_input}", key="facts")], "dive")
        fake_llm.script([tool_call("delegate_to_agent", agent_id=workflow["id"], task="Linear"), "The dive is done."])

        events = await _lead_turn(lead)

        assert "dove into Linear" in _delegate_result(events)
        assert len(await _runs_of("orch_f262")) == 1
        assert len(fake_llm.calls) == 2      # the lead's two turns; no chat loop for the workflow agent

    async def test_a_failure_is_reported_as_one(self, fake_llm):
        lead, workflow = await _team("orch_f262f", [
            {"id": "boom", "name": "Boom", "type": "transform", "transform_code": "raise ValueError('no data')",
             "output_key": "x", "next_step_id": None}], "boom")
        fake_llm.script([tool_call("delegate_to_agent", agent_id=workflow["id"], task="Linear"), "It failed."])

        result = _delegate_result(await _lead_turn(lead))

        assert "failed" in result.lower() and "no data" in result

    async def test_a_pause_says_where_to_answer_and_is_not_the_leads(self, fake_llm):
        lead, workflow = await _team("orch_f262p", [_human("ask")], "ask")
        fake_llm.script([tool_call("delegate_to_agent", agent_id=workflow["id"], task="Linear"), "Waiting."])

        events = await _lead_turn(lead)

        result = _delegate_result(events)
        runs = await _runs_of("orch_f262p")
        assert len(runs) == 1 and runs[0]["status"] == "paused"
        assert runs[0]["run_id"] in result and "ok?" in result
        assert not any(e.get("type") == "human_input_required" for e in events)


class TestAWorkflowDoesNotDelegateIntoItself:
    async def test_the_second_entry_is_refused(self, fake_llm):
        from core.react_engine import run_orchestrator_agent
        lead = S.make_agent(id="lead_f262loop", name="Loop Lead", type="delegate", tools=[],
                            delegate_agent_ids=["orch_agent_orch_f262loop"])
        orch = S.make_orchestration(id="orch_f262loop", entry_step_id="work", steps=[
            {"id": "work", "name": "Work", "type": "agent", "agent_id": lead["id"], "prompt_template": "go",
             "output_key": "out", "next_step_id": None}])
        workflow = S.make_orchestrator_agent("orch_f262loop", id="orch_agent_orch_f262loop", name="Loop")
        await S.seed_orchestrations([orch])
        await S.seed_agents([lead, workflow])
        fake_llm.script([tool_call("delegate_to_agent", agent_id=workflow["id"], task="again"), "stopped"])

        events = [e async for e in run_orchestrator_agent(workflow, "go", _server())]

        previews = [e.get("preview") or "" for e in events
                    if e.get("type") == "tool_result" and e.get("tool_name") == "delegate_to_agent"]
        assert previews and "already running" in previews[0]
        assert len(await _runs_of("orch_f262loop")) == 1
