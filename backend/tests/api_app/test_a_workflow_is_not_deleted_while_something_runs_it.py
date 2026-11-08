"""
A workflow is not deleted while something runs it, and a workflow agent must run one that exists (F408).

C2 measured it on cloud: a deployed workflow deleted with its agent left in the
chat picker, and an agent saved pointing at a workflow the workspace does not
have — both answered every message with "Orchestration 'orch_…' not found", as
would every widget or channel bound to them. Here the self-hosted product's
doors: the delete route, the builder's delete tool, the agent save, and a turn
on an agent whose workflow is already gone (or, as cloud keeps it, retired).
"""
import pytest

from _fakes import seed as S


def _agent(**extra) -> dict:
    return {"name": "Invoice desk", "description": "d", "type": "orchestrator", "tools": [],
            "system_prompt": "x"} | extra


async def _deployed(orch_id="orch_inv", name="Invoice triage", agent_id="agent_inv", agent_name="Invoice desk"):
    await S.seed_orchestrations([S.make_orchestration(id=orch_id, name=name)])
    await S.seed_agents([S.make_agent(id=agent_id, name=agent_name, type="orchestrator", orchestration_id=orch_id)])


class TestTheDeleteRoute:
    async def test_refuses_while_its_agent_runs_it_and_names_what_uses_the_agent(self, client):
        await _deployed()
        from core.chat_widgets.store import save_widget
        from core.store import schedules
        await save_widget({"name": "Support", "agent_id": "agent_inv"})
        await schedules.save({"id": "sched_morning", "name": "Every morning", "target_type": "agent",
                              "target_id": "agent_inv", "cron": "0 9 * * *"})

        resp = await client.delete("/api/orchestrations/orch_inv")

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail.startswith("'Invoice triage' was not deleted")
        assert "the agent 'Invoice desk' runs it" in detail
        assert "the widget 'Support'" in detail and "the schedule 'Every morning'" in detail
        assert (await client.get("/api/orchestrations/orch_inv")).status_code == 200

    async def test_refuses_while_a_schedule_runs_it(self, client):
        await S.seed_orchestrations([S.make_orchestration(id="orch_nightly", name="Nightly report")])
        from core.store import schedules
        await schedules.save({"id": "sched_n", "name": "Nightly", "target_type": "orchestration",
                              "target_id": "orch_nightly", "cron": "0 2 * * *"})

        resp = await client.delete("/api/orchestrations/orch_nightly")

        assert resp.status_code == 409
        assert "the schedule 'Nightly' runs it" in resp.json()["detail"]

    async def test_names_the_workflow_that_calls_it_through_its_agent(self, client):
        await _deployed(orch_id="orch_child", name="Pricing", agent_id="agent_child", agent_name="Pricing agent")
        await S.seed_orchestrations([
            S.make_orchestration(id="orch_child", name="Pricing"),
            S.make_orchestration(id="orch_parent", name="Month end", steps=[
                {"id": "p1", "name": "Price", "type": "agent", "agent_id": "agent_child"}]),
        ])

        resp = await client.delete("/api/orchestrations/orch_child")

        assert resp.status_code == 409
        assert "the workflow 'Month end' uses that agent" in resp.json()["detail"]

    async def test_one_nothing_runs_is_deleted(self, client, seed_orchestration):
        orch = await seed_orchestration(name="Loose")
        assert (await client.delete(f"/api/orchestrations/{orch['id']}")).status_code == 200
        assert (await client.get(f"/api/orchestrations/{orch['id']}")).status_code == 404

    async def test_one_that_does_not_exist_is_a_404(self, client):
        """It answered `success` for any id."""
        assert (await client.delete("/api/orchestrations/orch_nowhere")).status_code == 404


class TestTheBuildersDelete:
    async def test_refuses_in_the_same_words(self):
        import json
        from types import SimpleNamespace
        from core.builder_tools import execute_builder_tool

        await _deployed()
        result = json.loads(await execute_builder_tool(
            "delete_orchestration", {"orch_id": "orch_inv", "confirm": True}, SimpleNamespace(agent_sessions={})))

        assert result["error"].startswith("'Invoice triage' was not deleted")
        from core.store.resources import get_orchestration
        assert await get_orchestration("orch_inv") is not None


class TestTheAgentSave:
    async def test_refuses_a_workflow_that_does_not_exist(self, client):
        resp = await client.post("/api/agents", json=_agent(id="agent_bad", orchestration_id="orch_does_not_exist"))
        assert resp.status_code == 422
        assert "does not exist in this workspace" in resp.json()["detail"]

    async def test_refuses_a_retired_one(self, client):
        await S.seed_orchestrations([S.make_orchestration(id="orch_retired", is_active=False)])
        resp = await client.post("/api/agents", json=_agent(id="agent_r", orchestration_id="orch_retired"))
        assert resp.status_code == 422

    async def test_saves_one_that_exists(self, client):
        await S.seed_orchestrations([S.make_orchestration(id="orch_real")])
        assert (await client.post("/api/agents", json=_agent(id="agent_ok", orchestration_id="orch_real"))
                ).status_code == 200

    async def test_an_agent_whose_workflow_went_before_can_still_be_saved_as_it_is(self, client):
        """Only a changed pointer is checked: nobody is trapped with an agent they cannot rename."""
        await S.seed_agents([S.make_agent(id="agent_old", type="orchestrator", orchestration_id="orch_gone")])
        resp = await client.post("/api/agents", json=_agent(id="agent_old", name="Renamed", orchestration_id="orch_gone"))
        assert resp.status_code == 200


class TestATurnOnAnAgentWhoseWorkflowIsGone:
    @pytest.mark.parametrize("stored", [None, {"is_active": False}])
    async def test_names_the_agent_and_runs_nothing(self, stored, monkeypatch):
        """Missing (self-hosted deletes the row) or retired (cloud keeps it): neither runs."""
        from core import react_engine
        from core.scale import context

        async def resolve(orch_id):
            return None if stored is None else {"id": orch_id, "name": "Old", "steps": [], **stored}

        monkeypatch.setattr(context, "resolve_orchestration", resolve)
        events = [e async for e in react_engine.run_orchestrator_agent(
            {"id": "agent_inv", "name": "Invoice desk", "type": "orchestrator", "orchestration_id": "orch_inv"},
            "hello", None)]

        assert events == [{"type": "error", "message": react_engine.workflow_gone({"name": "Invoice desk"})}]
        assert "'Invoice desk'" in events[0]["message"] and "orch_inv" not in events[0]["message"]


class TestTheSentence:
    def test_several_runners(self):
        from core.orchestration.dependents import Dependent, refusal
        text = refusal("Report", [Dependent("agent", "A", id="a1"), Dependent("channel", "Ops", via="a1"),
                                  Dependent("workflow", "Parent", via="a1"), Dependent("schedule", "Daily")])
        assert text == ("'Report' was not deleted: the agent 'A' runs it, and the channel 'Ops' and the workflow "
                        "'Parent' use that agent; the schedule 'Daily' runs it. Point them at another workflow, "
                        "or delete them, first.")
