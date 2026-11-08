"""A tool a workflow's step uses is never deleted from under it (F1348, REACH1; CAP3's ff18-red fix-01; Naveen 11-04).

The change route's delete review read what uses a tool from agents' tool lists alone, so it said "nothing uses it" of
a tool a workflow's Tool step called, and the delete was approved; the Tools screen's delete and the builder's read
nothing at all. A step whose tool is gone fails every run after. These pin the class, at every door that deletes one:

- what uses a tool is every live workflow step that calls it (a Tool step) or holds its agent to it (allowed_tools),
  and every agent that holds it (`dependents.what_uses_tool`);
- a step's use refuses the delete, naming each workflow and its step (`dependents.tool_refusal`, Naveen: "refuse");
- agents alone never refuse: the delete goes ahead, the review naming each one;
- the change route, the Tools screen and the builder's delete_custom_tool read it and say it the same way.
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S

TOOL = "order_lookup"


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


async def _a_tool():
    from core.store.resources import save_tools

    await save_tools([{"name": TOOL, "description": "Look up an order.", "tool_type": "http",
                       "url": "https://shop.example/orders/{id}", "method": "GET"}])


async def _still_there() -> bool:
    from core.store.resources import load_tools

    return any(t.get("name") == TOOL for t in await load_tools())


def _calling(orch_id: str, name: str, step: dict) -> dict:
    return S.make_orchestration(id=orch_id, name=name, entry_step_id=step["id"], steps=[step])


@pytest.fixture
async def a_step_calls_it(client):
    await _a_tool()
    await S.seed_orchestrations([_calling("orch_status", "Order status check", {
        "id": "look", "name": "Look up order", "type": "tool", "forced_tool": TOOL, "tool_arguments": {},
        "output_key": "order", "next_step_id": None})])


class TestWhatUsesATool:
    async def test_a_tool_step_an_allowed_tools_step_and_an_agent(self, client):
        from core.orchestration import dependents

        await _a_tool()
        await S.seed_agents([S.make_agent(id="agent_support", name="Support", tools=[TOOL])])
        await S.seed_orchestrations([
            _calling("orch_status", "Order status check", {"id": "look", "name": "Look up order", "type": "tool",
                                                           "forced_tool": TOOL, "output_key": "o", "next_step_id": None}),
            _calling("orch_reply", "Reply to customers", {"id": "ans", "name": "Answer", "type": "agent",
                                                          "agent_id": "agent_support", "allowed_tools": [TOOL],
                                                          "output_key": "a", "next_step_id": None})])
        found = {(d.kind, d.name, d.via) for d in await dependents.what_uses_tool(TOOL)}
        assert found == {("step", "Order status check", "Look up order"), ("step", "Reply to customers", "Answer"),
                         ("agent", "Support", None)}

    async def test_a_workflow_deleted_and_kept_as_a_record_uses_nothing(self, client):
        from core.orchestration import dependents

        gone = _calling("orch_old", "Old", {"id": "s", "name": "S", "type": "tool", "forced_tool": TOOL,
                                            "output_key": "o", "next_step_id": None})
        await S.seed_orchestrations([{**gone, "is_active": False}])
        assert await dependents.what_uses_tool(TOOL) == []


class TestAStepsUseRefusesTheDelete:
    REFUSED = ("'order_lookup' was not deleted: the step 'Look up order' of the workflow 'Order status check' uses "
               "it. Point it at another tool, or remove it, first.")

    async def test_the_change_routes_review_refuses_it_naming_the_workflow_and_step(self, a_step_calls_it):
        from core.native_builder import change_ops as C
        from core.native_builder.change_tools import _used_by

        used, refusal = await _used_by(C.OPS["delete_tool"], TOOL)
        assert 'workflow "Order status check" (step "Look up order")' in used, used
        assert refusal == self.REFUSED

    async def test_the_tools_screen_refuses_it(self, client, a_step_calls_it):
        refused = await client.delete(f"/api/tools/custom/{TOOL}")
        assert refused.status_code == 409 and refused.json()["detail"] == self.REFUSED, refused.text
        assert await _still_there()

    async def test_the_builders_delete_refuses_it(self, client, a_step_calls_it):
        from core.builder_tools import execute_builder_tool_with_events

        raw, _ = await execute_builder_tool_with_events("delete_custom_tool", {"name": TOOL, "confirm": True},
                                                        _Server())
        assert json.loads(raw).get("error") == self.REFUSED, raw
        assert await _still_there()


class TestAgentsAloneNeverRefuse:
    async def test_the_delete_goes_ahead_and_the_review_names_them(self, client):
        from core.native_builder import change_ops as C
        from core.native_builder.change_tools import _used_by

        await _a_tool()
        await S.seed_agents([S.make_agent(id="agent_support", name="Support", tools=[TOOL])])
        used, refusal = await _used_by(C.OPS["delete_tool"], TOOL)
        assert used == ['agent "Support"'] and refusal is None
        assert (await client.delete(f"/api/tools/custom/{TOOL}")).status_code == 200
        assert not await _still_there()

    async def test_an_unused_tool_deletes_as_before(self, client):
        from core.native_builder import change_ops as C
        from core.native_builder.change_tools import _used_by

        await _a_tool()
        assert await _used_by(C.OPS["delete_tool"], TOOL) == ([], None)
        assert (await client.delete(f"/api/tools/custom/{TOOL}")).status_code == 200
