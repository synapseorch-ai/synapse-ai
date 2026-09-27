"""Approving a delegate's gated tool call runs it, as the delegate (F286).

A delegate's call held for approval was parked under the lead, with the lead's
snapshot and the delegate's call as the "pending call". Approving it resumed
the lead, which replayed the call as its own: blocked ("Tool not available
for this agent") when the lead did not hold the tool, or — worse — run under
the lead's identity and policy when it did. The delegate itself was never
resumed.

Now what is parked is the lead's own `delegate_to_agent` call, with the
delegate's approval and transcript inside it. Approving it replays the
delegation, and the delegate picks its own turn up again and runs (or is
refused) the call under its own tools and policy. Where nobody can answer —
a messaging channel — the lead is told so and carries on.
"""
import types
import unittest.mock as _m

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call

LEAD, HELPER = "lead_f286", "helper_f286"
DANGER = {"type": "function", "function": {"name": "danger", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}}}


def _server():
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


@pytest.fixture
async def team():
    await S.seed_agents([
        S.make_agent(id=HELPER, name="Helper", tools=["all"], skip_default_tools=True,
                     tool_policy={"undeclared": "confirm"}),
        S.make_agent(id=LEAD, name="Lead", type="delegate", tools=[], delegate_agent_ids=[HELPER]),
    ])


@pytest.fixture
def helper_tools():
    """Only the helper holds `danger`; what ran, and as whom, is recorded."""
    import core.react_engine as re_mod
    real = re_mod.run_agent_step
    executed: list[tuple[str, str]] = []

    async def wrapped(**kwargs):
        if kwargs.get("agent_id") == HELPER:
            async def executor(name, args):
                executed.append((HELPER, name))
                return f"ran-{name}"
            kwargs.setdefault("tools_override", [DANGER])
            kwargs.setdefault("tool_executor", executor)
        async for ev in real(**kwargs):
            yield ev

    with _m.patch.object(re_mod, "run_agent_step", wrapped):
        yield executed


async def _lead(fake_llm, script, resume_state=None, progress_events=True):
    import core.react_engine as re_mod
    fake_llm.script(script)
    return [e async for e in re_mod.run_agent_step(
        message="clean up", agent_id=LEAD, session_id="s_f286", server_module=_server(), max_turns=4,
        progress_events=progress_events, resume_state=resume_state)]


def _parked(events):
    """What chat parks: the approval event, and the last snapshot (never forwarded)."""
    from core.chat_approval import parked_payload
    card = next(e for e in events if e.get("type") == "human_input_required")
    snaps = [{k: v for k, v in e.items() if k != "type"} for e in events if e.get("type") == "_step_progress"]
    return card, parked_payload(card, message="clean up", progress=snaps[-1] if snaps else None)


class TestTheLeadStopsAtItsDelegatesQuestion:
    async def test_the_card_is_the_delegates_call_and_nothing_ran(self, team, helper_tools, fake_llm):
        events = await _lead(fake_llm, [tool_call("delegate_to_agent", agent_id=HELPER, task="clean"),
                                        tool_call("danger"), "helper: should not get here", "lead: nor here"])

        card, parked = _parked(events)
        assert card["tool_name"] == "danger" and card.get("delegate_from") == LEAD
        assert helper_tools == []
        assert parked["progress"]["delegate_pending"]["agent_id"] == HELPER
        # The delegate's transcript is engine state: never on the card itself.
        assert "delegate_pending" not in card and "progress" not in card
        assert events[-1]["type"] == "human_input_required"        # the lead stopped there


class TestApprovingRunsItAsTheDelegate:
    async def test_an_approval(self, team, helper_tools, fake_llm):
        from core.chat_approval import resume_state_for
        first = await _lead(fake_llm, [tool_call("delegate_to_agent", agent_id=HELPER, task="clean"),
                                       tool_call("danger")])
        _card, parked = _parked(first)

        events = await _lead(fake_llm, ["helper: cleaned", "lead: all done"],
                             resume_state=resume_state_for(parked, approve=True))

        assert helper_tools == [(HELPER, "danger")]                 # run once, by the helper
        result = next(e["preview"] for e in events if e.get("type") == "tool_result"
                      and e.get("tool_name") == "delegate_to_agent")
        assert "helper: cleaned" in result
        assert not any("not available" in (e.get("preview") or "") for e in events)
        assert next(e["response"] for e in events if e.get("type") == "final" and not e.get("delegate_from")) \
            == "lead: all done"

    async def test_a_refusal(self, team, helper_tools, fake_llm):
        from core.chat_approval import resume_state_for
        first = await _lead(fake_llm, [tool_call("delegate_to_agent", agent_id=HELPER, task="clean"),
                                       tool_call("danger")])
        _card, parked = _parked(first)

        events = await _lead(fake_llm, ["helper: I could not", "lead: noted"],
                             resume_state=resume_state_for(parked, approve=False, reason="not today"))

        assert helper_tools == []
        # The delegate is told, in its own turn, that its call was refused and why.
        helper_turn = fake_llm.calls[-2]
        text = str(helper_turn.get("prompt_msg") or helper_turn)
        assert "refused the call to 'danger'" in text and "not today" in text
        result = next(e["preview"] for e in events if e.get("type") == "tool_result"
                      and e.get("tool_name") == "delegate_to_agent")
        assert "helper: I could not" in result

    async def test_the_approval_is_the_leads_call_not_the_delegates(self, team, helper_tools, fake_llm):
        """The lead's resume state approves its own `delegate_to_agent`, never the
        delegate's call — or the lead could later run that call as itself."""
        from core.chat_approval import resume_state_for
        from core.react_engine import _approval_id
        first = await _lead(fake_llm, [tool_call("delegate_to_agent", agent_id=HELPER, task="clean"),
                                       tool_call("danger")])
        _card, parked = _parked(first)

        state = resume_state_for(parked, approve=True)

        assert state["pending_call"]["tool"] == "delegate_to_agent"
        assert _approval_id("danger", {}) not in state["approved_calls"]
        assert _approval_id("delegate_to_agent", {"agent_id": HELPER, "task": "clean"}) in state["approved_calls"]


class TestWhereNobodyCanAnswer:
    async def test_the_lead_is_told_and_carries_on(self, team, helper_tools, fake_llm):
        from core.chat_approval import nobody_to_ask
        with nobody_to_ask():
            events = await _lead(fake_llm, [tool_call("delegate_to_agent", agent_id=HELPER, task="clean"),
                                            tool_call("danger"), "lead: carried on"], progress_events=False)

        assert helper_tools == []
        result = next(e["preview"] for e in events if e.get("type") == "tool_result"
                      and e.get("tool_name") == "delegate_to_agent")
        assert "approval" in result
        assert events[-1].get("type") == "final" and events[-1]["response"] == "lead: carried on"


class TestInAnOrchestrationStep:
    async def test_an_approval_runs_it_as_the_delegate(self, team, helper_tools, fake_llm):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        orch = S.make_orchestration(id="orch_f286", entry_step_id="lead", steps=[
            {"id": "lead", "name": "Lead", "type": "agent", "agent_id": LEAD, "prompt_template": "clean up",
             "output_key": "out", "max_turns": 4, "next_step_id": None}])
        await S.seed_orchestrations([orch])
        fake_llm.script([tool_call("delegate_to_agent", agent_id=HELPER, task="clean"), tool_call("danger")])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        first = [e async for e in engine.run("go", run_id="run_f286")]
        assert first[-1]["type"] == "human_input_required" and helper_tools == []

        fake_llm.script(["helper: cleaned", "lead: all done"])
        [e async for e in OrchestrationEngine.resume("run_f286", {"approve": True}, _server())]

        from core.orchestration.state import SharedState
        run = (await SharedState.restore("run_f286")).run
        assert helper_tools == [(HELPER, "danger")]
        assert run.status == "completed" and run.shared_state["out"] == "lead: all done"
