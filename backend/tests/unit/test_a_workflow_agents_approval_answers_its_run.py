"""Approving a workflow agent's gated call in chat answers the run that asked (F290).

Chatting with a workflow agent whose step held a tool call for approval, the
card was parked as a chat approval — the engine's event named no run — and
"Allow" re-ran the chat turn: the workflow started again from the original
message as a new run, and the run that asked stayed paused for ever.

Now the question names its run, the parked approval keeps it, and answering
resumes that run with the decision — the call runs once, in the run that asked.
"""
import types
import unittest.mock as _m

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call

WORKER = "agent_f290_worker"
DANGER = {"type": "function", "function": {"name": "danger", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}}}


@pytest.fixture
async def workflow_agent():
    orch = S.make_orchestration(id="orch_f290", name="Cleanup", entry_step_id="work", steps=[
        {"id": "work", "name": "Do the work", "type": "agent", "agent_id": WORKER, "prompt_template": "go",
         "output_key": "result", "max_turns": 4, "next_step_id": None}])
    agent = S.make_orchestrator_agent("orch_f290", id="orch_agent_f290", name="Cleanup")
    await S.seed_orchestrations([orch])
    await S.seed_agents([agent, S.make_agent(id=WORKER, tools=["all"], skip_default_tools=True,
                                             tool_policy={"undeclared": "confirm"})])
    return agent


@pytest.fixture
def executed():
    import core.react_engine as re_mod
    real = re_mod.run_agent_step
    ran: list[str] = []

    async def wrapped(**kwargs):
        if kwargs.get("agent_id") == WORKER:
            async def executor(name, args):
                ran.append(name)
                return f"ran-{name}"
            kwargs.setdefault("tools_override", [DANGER])
            kwargs.setdefault("tool_executor", executor)
        async for ev in real(**kwargs):
            yield ev

    with _m.patch.object(re_mod, "run_agent_step", wrapped):
        yield ran


async def _runs():
    from core.orchestration.state import SharedState
    return [r for r in await SharedState.list_runs(limit=20) if r["orchestration_id"] == "orch_f290"]


class TestAllowAnswersTheRunThatAsked:
    async def test_the_call_runs_once_in_the_same_run(self, client, workflow_agent, executed, fake_llm):
        fake_llm.script([tool_call("danger"), "worker: done"])
        resp = await client.post("/chat/stream", json={"message": "clean up", "agent_id": workflow_agent["id"],
                                                       "session_id": "s_f290"})
        assert resp.status_code == 200 and '"tool_approval"' in resp.text
        [paused] = await _runs()
        assert paused["status"] == "paused" and executed == []

        resp = await client.post("/chat/s_f290/approve", json={"approve": True, "agent_id": workflow_agent["id"]})

        assert resp.status_code == 200
        runs = await _runs()
        assert [r["run_id"] for r in runs] == [paused["run_id"]]     # no second run
        assert runs[0]["status"] == "completed"
        assert executed == ["danger"]
