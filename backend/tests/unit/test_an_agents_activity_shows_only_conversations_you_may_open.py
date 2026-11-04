"""An agent's activity shows only the conversations you may open (F1361, REACH1; eb 11-04).

`agent_activity` (Synapse AI's topic, the assistant's and MCP's tool: one handler) carries an agent's recent
conversations with their questions and answers. Which conversations a person may open is the host's rule, as its Chat
decides it; this read had none. These pin the class:

- with no rule (self-hosted, one person), every person's conversation is read as before;
- a host's rule (`activity.set_conversation_rule`) is asked before any conversation is read: one it hides is in no
  field, its history is never read, and it takes no place among the ones shown;
- the rule is asked for the caller the read acts for, the run's person in Synapse AI's run;
- a rule that cannot answer refuses the read, never shows everything.
"""
from __future__ import annotations

import json

import pytest

from core.product_tools import activity, areas, registry
from core.product_tools.caller import Caller
from core.product_tools.errors import ToolRefusal
from core.product_tools.roles import Role

PERSON = Caller(role=Role.VIEWER, via="mcp", user_id="u1")


@pytest.fixture(autouse=True)
def no_rule():
    activity.set_conversation_rule(None)
    yield
    activity.set_conversation_rule(None)


@pytest.fixture
async def six_conversations(seed_agent):
    """Support bot with six person conversations (one more than a read shows), each with its own words."""
    from core.store import sessions

    bot = await seed_agent(id="agent_support", name="Support bot")
    for i in range(6):
        await sessions.append_turn(f"chat_{i}", bot["id"], [
            {"role": "user", "content": f"question number {i}?"},
            {"role": "assistant", "content": f"answer number {i}."}])
    return bot


async def _activity(caller: Caller = PERSON) -> dict:
    areas.ensure_loaded()
    return await registry.invoke("agent_activity", caller, {"name": "support"})


def _asked(got: dict) -> set[str]:
    return {talk["last_question"] for talk in got["conversations"]}


class TestWithNoRule:
    async def test_every_persons_conversation_is_read_as_before(self, six_conversations):
        got = await _activity()
        assert len(got["conversations"]) == 5
        assert _asked(got) <= {f"question number {i}?" for i in range(6)}


class TestARuleHidesOne:
    async def test_it_is_in_no_field_never_read_and_takes_no_place(self, six_conversations, monkeypatch):
        from core import session

        async def hides_chat_3(caller, session_ids):
            return {s for s in session_ids if s == "chat_3"}

        activity.set_conversation_rule(hides_chat_3)
        read = []
        real = session._get_conversation_history

        async def spied(session_id, *args, **kwargs):
            read.append(session_id)
            return await real(session_id, *args, **kwargs)

        monkeypatch.setattr(session, "_get_conversation_history", spied)
        got = await _activity()

        assert _asked(got) == {f"question number {i}?" for i in (0, 1, 2, 4, 5)}
        assert "number 3" not in json.dumps(got)
        assert "chat_3" not in read and read


class TestTheRuleIsAskedForTheCallerTheReadActsFor:
    async def test_the_mcp_callers_and_synapse_ais_runs_person(self, six_conversations):
        from core import builder_access
        from core.builder_tools import execute_builder_tool_with_events
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.state import SharedState
        from core.product_tools import hooks

        asked = []

        async def records(caller, session_ids):
            asked.append(caller.user_id)
            return set()

        activity.set_conversation_rule(records)
        await _activity()

        async def roles(user_id):
            return Role.VIEWER if user_id == "v1" else None

        class _Server:
            agent_sessions: dict = {}
            tool_router: dict = {}

        hooks.set_role_source(roles)
        builder_access.forget_runs()
        try:
            await SharedState(OrchestrationRun(run_id="builder_v", orchestration_id="orch_native_builder",
                                               shared_state={builder_access.CALLER_KEY: {"user_id": "v1"}})
                              ).checkpoint()
            raw, _ = await execute_builder_tool_with_events(
                "read_workspace", {"topic": "agent_activity", "name": "support"}, _Server(), run_id="builder_v")
        finally:
            hooks.set_role_source(None)
            builder_access.forget_runs()
        assert json.loads(raw)["conversations"], raw
        assert asked == ["u1", "v1"]


class TestARuleThatCannotAnswer:
    async def test_refuses_the_read(self, six_conversations):
        async def broken(caller, session_ids):
            raise RuntimeError("the owners table is not there")

        activity.set_conversation_rule(broken)
        with pytest.raises(ToolRefusal) as refused:
            await _activity()
        assert "number" not in str(refused.value)
