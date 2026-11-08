"""
An agent is not deleted while something uses it (F467).

F408's other half, one level down. Deleting an agent deleted the row and its
memory and nothing else: a widget bound to it went on answering its visitors —
with an error in cloud — a channel or a phone number went silent, a schedule
fired at an agent that was gone, a workflow step that names it ran nothing (or,
self-hosted, quietly ran whichever agent sorts first), and an agent that hands
work to it lost a helper without a word. F408's refusal sends a person who
wants a workflow gone to delete its agent next, and this is what they met.

Naveen's call (D276): refuse, as F408 does — 409, naming each thing that uses
the agent — at every door that deletes one: the route and the builder's tool.
"""
import json
from types import SimpleNamespace

import pytest

from _fakes import seed as S


async def _desk(agent_id="agent_desk", name="Front desk"):
    await S.seed_agents([S.make_agent(id=agent_id, name=name)])


async def _kept(client, agent_id="agent_desk") -> bool:
    return agent_id in {a["id"] for a in (await client.get("/api/agents")).json()}


async def _builder(tool, args):
    from core.builder_tools import execute_builder_tool
    return json.loads(await execute_builder_tool(tool, args, SimpleNamespace(agent_sessions={})))


class TestTheDeleteRoute:
    async def test_refuses_while_a_widget_a_channel_and_a_phone_number_use_it(self, client):
        await _desk()
        from core.chat_widgets.store import save_widget
        from core.messaging import store as messaging
        await save_widget({"name": "Support", "agent_id": "agent_desk"})
        await messaging.save_channel({"id": "ch_tg", "name": "Team Telegram", "platform": "telegram",
                                      "agent_id": "agent_desk", "config": {}})
        await messaging.save_channel({"id": "ch_ph", "name": "Main line", "platform": "twilio_voice",
                                      "agent_id": "agent_desk", "config": {}})

        resp = await client.delete("/api/agents/agent_desk")

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail.startswith("'Front desk' was not deleted: ")
        assert "the widget 'Support'" in detail
        assert "the channel 'Team Telegram'" in detail
        assert "the phone number 'Main line'" in detail
        assert detail.endswith("Point them at another agent, or delete them, first.")
        assert await _kept(client)

    async def test_refuses_while_a_schedule_runs_it(self, client):
        await _desk()
        from core.store import schedules
        await schedules.save({"id": "sched_m", "name": "Every morning", "target_type": "agent",
                              "target_id": "agent_desk", "cron": "0 9 * * *"})

        resp = await client.delete("/api/agents/agent_desk")

        assert resp.status_code == 409
        assert "the schedule 'Every morning' runs it" in resp.json()["detail"]

    async def test_refuses_while_a_workflows_step_runs_it(self, client):
        await _desk()
        await S.seed_orchestrations([S.make_orchestration(id="orch_mon", name="Month end", steps=[
            {"id": "p1", "name": "Ask", "type": "agent", "agent_id": "agent_desk"}])])

        resp = await client.delete("/api/agents/agent_desk")

        assert resp.status_code == 409
        assert "the workflow 'Month end' runs it in a step" in resp.json()["detail"]

    async def test_refuses_while_another_agent_hands_work_to_it(self, client):
        """Unbinding it there would not do: an empty list means the router may use any agent."""
        await S.seed_agents([S.make_agent(id="agent_desk", name="Front desk"),
                             S.make_agent(id="agent_router", name="Router", type="delegate",
                                          delegate_agent_ids=["agent_desk"])])

        resp = await client.delete("/api/agents/agent_desk")

        assert resp.status_code == 409
        assert "the agent 'Router' hands work to it" in resp.json()["detail"]

    async def test_a_deleted_workflow_calling_it_does_not_hold_it(self, client):
        """A workflow kept as a record (`is_active` false) runs nothing (F423)."""
        await _desk()
        await S.seed_orchestrations([S.make_orchestration(id="orch_old", name="Old", is_active=False, steps=[
            {"id": "p1", "name": "Ask", "type": "agent", "agent_id": "agent_desk"}])])

        assert (await client.delete("/api/agents/agent_desk")).status_code == 200

    async def test_a_workflow_agent_whose_own_workflow_calls_it_is_deleted_and_then_the_workflow(self, client):
        """F690 (BWA1, 2026-10-16; Naveen: let the cycle's delete through): the builder wired a workflow's step to
        the workflow agent that runs that same workflow (F691). Each delete was refused because of the other — the
        workflow because the agent runs it (F408), the agent because the workflow runs it in a step — so a person
        could delete neither. The workflow an agent runs no longer holds that agent: the agent goes, then the
        workflow can. Any other workflow calling it still holds it."""
        await S.seed_orchestrations([S.make_orchestration(id="orch_loop", name="Billing", steps=[
            {"id": "s1", "name": "Bill", "type": "agent", "agent_id": "agent_loop"}])])
        await S.seed_agents([S.make_orchestrator_agent("orch_loop", id="agent_loop", name="Billing agent")])

        assert (await client.delete("/api/orchestrations/orch_loop")).status_code == 409, "an agent still runs it"
        assert (await client.delete("/api/agents/agent_loop")).status_code == 200
        assert (await client.delete("/api/orchestrations/orch_loop")).status_code == 200

    async def test_another_workflow_calling_a_workflow_agent_still_holds_it(self, client):
        await S.seed_orchestrations([
            S.make_orchestration(id="orch_own", name="Billing", steps=[{"id": "s1", "name": "End", "type": "end"}]),
            S.make_orchestration(id="orch_other", name="Month end", steps=[
                {"id": "s1", "name": "Bill", "type": "agent", "agent_id": "agent_wf"}])])
        await S.seed_agents([S.make_orchestrator_agent("orch_own", id="agent_wf", name="Billing agent")])

        resp = await client.delete("/api/agents/agent_wf")
        assert resp.status_code == 409 and "the workflow 'Month end' runs it in a step" in resp.json()["detail"]

    async def test_one_nothing_uses_is_deleted_with_its_memory(self, client):
        await _desk()
        from core import agent_memory
        await agent_memory.write_memory("agent_desk", "note", "remember this")

        resp = await client.delete("/api/agents/agent_desk")

        assert resp.status_code == 200
        assert not await _kept(client)
        assert agent_memory.read_memory("agent_desk", "note") is None


class TestTheBuildersDelete:
    async def test_refuses_in_the_same_words(self):
        await _desk()
        from core.chat_widgets.store import save_widget
        await save_widget({"name": "Support", "agent_id": "agent_desk"})

        result = await _builder("delete_agent", {"agent_id": "agent_desk", "confirm": True})

        assert result["error"].startswith("'Front desk' was not deleted: ")
        assert "the widget 'Support'" in result["error"]
        from core.store.resources import get_agent
        assert await get_agent("agent_desk") is not None

    async def test_one_nothing_uses_is_deleted(self):
        await _desk()
        result = await _builder("delete_agent", {"agent_id": "agent_desk", "confirm": True})
        assert "error" not in result, result
        from core.store.resources import get_agent
        assert await get_agent("agent_desk") is None


class TestTheSentence:
    def test_one_thing_reads_it_not_them(self):
        from core.orchestration.dependents import Dependent, agent_refusal
        said = agent_refusal("Front desk", [Dependent("widget", "Support")])
        assert said == ("'Front desk' was not deleted: the widget 'Support' uses it. "
                        "Point it at another agent, or delete it, first.")

    def test_each_kind_says_what_it_does(self):
        from core.orchestration.dependents import Dependent, agent_refusal
        said = agent_refusal("Front desk", [
            Dependent("widget", "Support"), Dependent("channel", "Team Telegram"),
            Dependent("schedule", "Every morning"), Dependent("workflow", "Month end"),
            Dependent("agent", "Router"),
        ])
        assert said == (
            "'Front desk' was not deleted: the widget 'Support' and the channel 'Team Telegram' use it; "
            "the schedule 'Every morning' runs it; the workflow 'Month end' runs it in a step; "
            "the agent 'Router' hands work to it. Point them at another agent, or delete them, first."
        )
