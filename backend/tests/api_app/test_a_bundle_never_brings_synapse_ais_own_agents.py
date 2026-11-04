"""A bundle never brings in Synapse AI's own agents or workflow (F1373, F1372's OSS twin; REACH1, eb 11-04).

A file exported from a 1.x install can carry the builder's own agents (`agent_native_builder*`) and its workflow with
no `system` mark. The import saved every one it was handed, under the file's ids, as the person's own: listed beside
their agents until the seeder took the earlier release's away, and one with this release's id written over the
builder's own. Known by its id alone, as the engine knows it (`seed.is_builder_agent`, F1351), so:

- each is left out, nothing of the builder's is written, and each is named in the import's warnings with why, in the
  sentence cloud's import says it with;
- a step of the file's that used one says so;
- a person's agent called "Builder …" imports as before.
"""
from __future__ import annotations

from typing import Any

from tests.api_app.test_a_bundle_travels import everything, warned

CREATOR = {"id": "agent_native_builder_creator", "name": "Agent Creator", "description": "Creates agents.",
           "model": "gpt-4.1-mini", "system_prompt": "You create agents."}
PERSONS = {"id": "agent_builder_reports", "name": "Builder of reports", "description": "Builds the weekly report.",
           "model": "gpt-4.1-mini", "system_prompt": "You build the weekly report."}
SYNAPSE_AIS_OWN = ("One of Synapse AI's own, from the install this file came from, so it isn't imported: "
                   "this workspace has its own Synapse AI.")


def _bundle(**parts: Any) -> dict[str, Any]:
    return {"synapse_export": True, "version": "1.1", "agents": [], "orchestrations": [], "mcp_servers": [],
            "custom_tools": [], **parts}


class TestSynapseAIsOwnAreLeftOut:
    async def test_its_agent_is_named_and_a_step_that_used_it_says_so(self, client: Any) -> None:
        from core.store.resources import get_agent, get_orchestration

        r = await client.post("/api/import", json=everything(_bundle(
            agents=[CREATOR, PERSONS],
            orchestrations=[{"id": "orch_both", "name": "Uses both", "entry_step_id": "make", "steps": [
                {"id": "make", "name": "Make", "type": "agent", "agent_id": CREATOR["id"], "output_key": "made",
                 "next_step_id": "report"},
                {"id": "report", "name": "Report", "type": "agent", "agent_id": PERSONS["id"],
                 "output_key": "report", "next_step_id": None}]}])))
        assert r.status_code == 200, r.text
        got = r.json()

        assert [a["id"] for a in got["results"]["agents"]] == [PERSONS["id"]]
        assert {"kind": "agents", "id": CREATOR["id"], "name": "Agent Creator",
                "message": SYNAPSE_AIS_OWN} in got["warnings"]
        assert warned(got, f"Its step Make uses the agent {CREATOR['id']}"), got["warnings"]
        assert await get_agent(CREATOR["id"]) is None
        assert [a["name"] for a in (await client.get("/api/agents")).json()
                if a["id"] in (CREATOR["id"], PERSONS["id"])] == ["Builder of reports"]
        steps = {s["id"]: s for s in (await get_orchestration("orch_both"))["steps"]}
        assert steps["make"]["agent_id"] == CREATOR["id"] and steps["report"]["agent_id"] == PERSONS["id"]

    async def test_its_workflow_is_named_and_nothing_of_the_builders_is_written(self, client: Any) -> None:
        from core.native_builder.seed import NATIVE_BUILDER_ORCH_ID
        from core.store.resources import get_orchestration

        before = await get_orchestration(NATIVE_BUILDER_ORCH_ID)
        r = await client.post("/api/import", json=everything(_bundle(orchestrations=[{
            "id": NATIVE_BUILDER_ORCH_ID, "name": "Native Builder", "entry_step_id": "say", "steps": [
                {"id": "say", "name": "Say", "type": "print", "print_content": "hi", "output_key": "said",
                 "next_step_id": None}]}])))
        assert r.status_code == 200, r.text
        got = r.json()

        assert got["results"]["orchestrations"] == []
        assert {"kind": "orchestrations", "id": NATIVE_BUILDER_ORCH_ID, "name": "Native Builder",
                "message": SYNAPSE_AIS_OWN} in got["warnings"]
        assert await get_orchestration(NATIVE_BUILDER_ORCH_ID) == before


class TestAPersonsAgentImportsAsBefore:
    async def test_one_called_builder_with_its_own_id(self, client: Any) -> None:
        r = await client.post("/api/import", json=everything(_bundle(agents=[PERSONS])))
        assert r.status_code == 200, r.text
        assert [a["id"] for a in r.json()["results"]["agents"]] == [PERSONS["id"]]
        assert not warned(r.json(), SYNAPSE_AIS_OWN), r.json()["warnings"]
        assert PERSONS["id"] in {a["id"] for a in (await client.get("/api/agents")).json()}
