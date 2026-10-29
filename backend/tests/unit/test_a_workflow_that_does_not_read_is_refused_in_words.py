"""A stored workflow the engine does not read is refused to a run in words, at every place a run loads one (F1235).

A human step saved as `{"fields": [...]}` (rc4.x's builder, every v1.9 release) made each run of its workflow fail
on pydantic's text — on the cloud the job died and was dead-lettered with it; here the run route answered 500. Every
load for a run now goes through `read_shapes.load_to_run`: the step by its name and id, the field, the shape it
needs and where to mend it, as F1047's save refusal says it. A route answers 422 with those words, MCP refuses with
them, and a run that stops on them keeps them whole as its reason.
"""
from __future__ import annotations

import types

import pytest

from _fakes import seed as S
from core.orchestration.read_shapes import Unreadable, load_to_run

HUMAN = {"id": "keys", "name": "Generate API Keys", "type": "human", "human_prompt": "Your API keys?",
         "human_fields": {"fields": [{"name": "api_key", "type": "str"}]}, "output_key": "keys",
         "next_step_id": None}
SAID = ('Step "Generate API Keys" (keys): `human_fields` must be a list of objects, '
        'e.g. [{"name": "email", "type": "string"}], not an object.')
WHERE = "Open it in the editor and save it, or ask Synapse AI to change it."


def _says_it(text: str) -> None:
    assert SAID in text and WHERE in text, text
    assert "validation error" not in text and "pydantic" not in text, text


@pytest.fixture
async def unreadable():
    await S.seed_orchestrations([S.make_orchestration(id="orch_odd", entry_step_id="keys", steps=[HUMAN])])
    return "orch_odd"


class TestTheWords:
    def test_load_to_run_says_the_step_the_field_and_where_to_mend_it(self):
        with pytest.raises(Unreadable) as e:
            load_to_run(S.make_orchestration(id="orch_odd", entry_step_id="keys", steps=[HUMAN]))
        assert str(e.value) == ("This workflow can't run: it was saved in a shape the engine does not read. "
                                f"{SAID} {WHERE}")

    def test_a_step_whose_name_is_its_id_is_named_once(self):
        with pytest.raises(Unreadable) as e:
            load_to_run(S.make_orchestration(steps=[{**HUMAN, "name": "keys"}], entry_step_id="keys"))
        assert "Step 'keys': `human_fields`" in str(e.value)

    def test_a_workflow_that_reads_is_loaded(self):
        assert load_to_run(S.make_orchestration(id="orch_ok")).id == "orch_ok"

    def test_a_run_keeps_the_words_whole_as_its_reason(self):
        from core.orchestration.run_error import reason_of

        many = [{**HUMAN, "id": f"keys_{i}", "name": f"Keys {i}"} for i in range(3)]
        with pytest.raises(Unreadable) as e:
            load_to_run(S.make_orchestration(steps=many, entry_step_id="keys_0"))
        assert len(str(e.value)) > 300
        assert reason_of(e.value) == str(e.value)

    def test_f1047_s_save_refusal_says_the_same_field_words(self):
        from core.builder_tools import _refuse_misshapen_steps

        field_words = SAID.removeprefix('Step "Generate API Keys" (keys): ')
        assert field_words.removesuffix(".") in _refuse_misshapen_steps([HUMAN])["error"]


class TestEachPlaceARunLoadsOne:
    async def test_the_run_route_answers_422(self, client, unreadable):
        resp = await client.post(f"/api/orchestrations/{unreadable}/run", json={"message": "go"})
        assert resp.status_code == 422, resp.text
        _says_it(resp.json()["detail"])

    @pytest.mark.parametrize("path", ["run", "run/stream"])
    async def test_v1_s_run_routes_answer_422(self, client, api_key, unreadable, path):
        resp = await client.post(f"/api/v1/orchestrations/{unreadable}/{path}", json={"message": "go"},
                                 headers=api_key["headers"])
        assert resp.status_code == 422, resp.text
        _says_it(resp.json()["detail"])

    async def test_v1_s_read_of_it_answers_422(self, client, api_key, unreadable):
        resp = await client.get(f"/api/v1/orchestrations/{unreadable}", headers=api_key["headers"])
        assert resp.status_code == 422, resp.text
        _says_it(resp.json()["detail"])

    async def test_mcp_refuses_it(self, unreadable):
        from core.mcp_server.runs import start_run
        from core.product_tools.errors import ToolRefusal

        with pytest.raises(ToolRefusal) as e:
            await start_run(unreadable, "go", None, 0)
        _says_it(str(e.value))

    async def test_a_schedule_s_run_stops_on_the_words(self, unreadable):
        from core.scheduler import ScheduleManager

        m = ScheduleManager()
        m._server_module = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
        with pytest.raises(Unreadable) as e:
            await m._run_orchestration_schedule({"id": "sched_odd", "target_id": unreadable, "prompt": "go"},
                                                types.SimpleNamespace())
        _says_it(str(e.value))

    async def test_an_agent_that_runs_it_says_it_in_the_chat(self, unreadable):
        from core.react_engine import run_orchestrator_agent

        agent = S.make_orchestrator_agent(unreadable, id="agent_odd")
        events = [e async for e in run_orchestrator_agent(agent, "go", types.SimpleNamespace(agent_sessions={}))]
        errors = [e["message"] for e in events if e.get("type") == "error"]
        assert errors, events
        _says_it(errors[0])

    async def test_a_workflow_step_whose_child_it_is_fails_on_the_words(self, unreadable):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        parent = S.make_orchestration(id="orch_parent", entry_step_id="child", steps=[
            {"id": "child", "name": "Run it", "type": "workflow", "workflow_id": unreadable, "next_step_id": None}])
        engine = OrchestrationEngine(Orchestration.model_validate(parent), types.SimpleNamespace(agent_sessions={}))
        events = [e async for e in engine.run("go", "run_parent_odd")]
        # The stream's `error` is the engine's sanitised sentence; the step's words ride its `reason` (F195).
        reasons = [e["reason"] for e in events if e.get("type") == "step_error" and e.get("reason")]
        assert reasons, events
        _says_it(reasons[0])

    async def test_a_paused_run_answered_says_it(self, unreadable):
        from core.orchestration.engine import OrchestrationEngine

        await S.seed_paused_run("run_odd_paused", unreadable)
        events = [e async for e in OrchestrationEngine.resume(
            "run_odd_paused", {"response": "x"}, types.SimpleNamespace(agent_sessions={}))]
        errors = [e["error"] for e in events if e.get("type") == "orchestration_error"]
        assert errors, events
        _says_it(errors[0])

    async def test_a_failed_run_replayed_says_it(self, unreadable):
        from core.orchestration.engine import OrchestrationEngine
        from core.store import get_store, upsert
        from core.store.models import OrchestrationRunDB

        async with (await get_store())() as session:
            await upsert(session, OrchestrationRunDB, values={
                "run_id": "run_odd_failed", "orchestration_id": unreadable, "status": "failed",
                "current_step_id": "keys"}, index_elements=["run_id"])
            await session.commit()
        events = [e async for e in OrchestrationEngine.resume_failed(
            "run_odd_failed", types.SimpleNamespace(agent_sessions={}))]
        errors = [e["error"] for e in events if e.get("type") == "orchestration_error"]
        assert errors, events
        _says_it(errors[0])

    async def test_the_cloud_worker_s_load_stops_its_job_on_the_words(self, unreadable):
        from core.orchestration.run_error import reason_of
        from core.scale import worker
        from core.store import get_store

        with pytest.raises(Unreadable) as e:
            await worker._load_orchestration(await get_store(), unreadable)
        _says_it(reason_of(e.value))
