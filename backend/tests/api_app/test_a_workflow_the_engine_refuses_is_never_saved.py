"""A workflow the engine would refuse to run as written is refused at every door that saves one — F850, F853
(BF11, D311).

Found by QA1b (D314) on prod: a Switch whose expression the engine refuses saved with no issue (F850), and an Agent
step pointed at its own workflow's agent saved 200 (F853, F691's loop). The builder's tools refused both; the
editor's save and an import checked neither. Each door now asks `core.orchestration.save_rules`, and says the
builder's words in a 422.
"""
from _fakes import seed as S

# QA1b's expression used `.splitlines()`; F957 offers it now, so the refused example is a method that stays out
# (`.zfill`: its size is the caller's). The rule tested is the same.
SPLIT = "'urgent' if 'urgent' in str(state.classification).lower().zfill(9) else 'routine'"


def _routed(oid, expression):
    return S.make_orchestration(id=oid, entry_step_id="step_route", steps=[
        {"id": "step_route", "name": "Route", "type": "switch", "switch_expression": expression,
         "switch_cases": {"urgent": "step_1"}, "switch_default_step_id": "step_1", "next_step_id": None},
        {"id": "step_1", "name": "Print", "type": "print", "print_content": "x", "output_key": "o",
         "next_step_id": None},
    ])


def _calling(oid, agent_id):
    return S.make_orchestration(id=oid, entry_step_id="step_research", steps=[
        {"id": "step_research", "name": "Research", "type": "agent", "agent_id": agent_id,
         "prompt_template": "go", "output_key": "o", "next_step_id": None},
    ])


def _bundle(orchestrations, agents=()):
    return {
        "bundle": {"synapse_export": True, "version": "1.1", "orchestrations": list(orchestrations),
                   "agents": list(agents), "mcp_servers": [], "custom_tools": []},
        "selected_orchestration_ids": [o["id"] for o in orchestrations],
        "selected_agent_ids": [a["id"] for a in agents],
        "selected_mcp_server_names": [],
        "selected_custom_tool_names": [],
    }


class TestTheEditorsSave:
    async def test_a_refused_expression_is_a_422_naming_the_step(self, client):
        resp = await client.post("/api/orchestrations", json=_routed("orch_f850_save", SPLIT))

        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail.startswith("Refusing to save:")
        assert "step 'step_route' switch_expression: .zfill(…) is not one of the methods available here" in detail
        assert (await client.get("/api/orchestrations/orch_f850_save")).status_code == 404

    async def test_an_expression_it_runs_saves(self, client):
        resp = await client.post("/api/orchestrations",
                                 json=_routed("orch_f850_ok", "str(state.classification).lower()"))
        assert resp.status_code == 200, resp.text

    async def test_a_step_on_its_own_workflow_agent_is_a_422(self, client, seed_agent):
        saved = await client.post("/api/orchestrations", json=S.make_orchestration(id="orch_f853"))
        assert saved.status_code == 200
        desk = await seed_agent(type="orchestrator", orchestration_id="orch_f853", name="Delay desk")

        resp = await client.post("/api/orchestrations", json=_calling("orch_f853", desk["id"]))

        assert resp.status_code == 422, resp.text
        assert (f"Agent step 'step_research' uses workflow agent '{desk['id']}', which runs this workflow"
                in resp.json()["detail"])
        stored = (await client.get("/api/orchestrations/orch_f853")).json()
        assert [s.get("agent_id") for s in stored["steps"]] != [desk["id"]]

    async def test_another_workflows_agent_saves(self, client, seed_agent):
        other = await seed_agent(type="orchestrator", orchestration_id="orch_elsewhere", name="Other desk")
        resp = await client.post("/api/orchestrations", json=_calling("orch_f853_ok", other["id"]))
        assert resp.status_code == 200, resp.text


class TestAnImport:
    async def test_a_refused_workflow_refuses_the_file_and_nothing_arrives(self, client):
        fine = S.make_orchestration(id="orch_f850_fine")
        resp = await client.post("/api/import", json=_bundle([fine, _routed("orch_f850_import", SPLIT)]))

        assert resp.status_code == 422, resp.text
        assert "step 'step_route' switch_expression" in resp.json()["detail"]
        for oid in ("orch_f850_fine", "orch_f850_import"):
            assert (await client.get(f"/api/orchestrations/{oid}")).status_code == 404

    async def test_a_loop_through_an_agent_in_the_same_file_is_refused(self, client):
        desk = S.make_agent(id="agent_f853_desk", type="orchestrator", orchestration_id="orch_f853_import")
        resp = await client.post("/api/import",
                                 json=_bundle([_calling("orch_f853_import", desk["id"])], [desk]))

        assert resp.status_code == 422, resp.text
        assert "which runs this workflow" in resp.json()["detail"]
