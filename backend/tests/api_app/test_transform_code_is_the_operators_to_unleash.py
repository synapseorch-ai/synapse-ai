"""The doors to a transform's code (F968, REL2's sizing; Naveen 2026-10-24).

Settings: `transform_runtime: "host"` — Python as the server, with the server's files — was one `POST /api/settings`
away for any client. It is now the server operator's choice alone (`SYNAPSE_TRANSFORM_RUNTIME=host`), and the
settings route refuses it by name rather than dropping it unsaid.

Import: a bundle's transform steps are code from a file, and nothing about them was said. Each one is now named in
the import's warnings, so it is read before the workflow runs.
"""
from tests.api_app.test_a_bundle_travels import everything

CODE = "import json\nresult = {'total': sum(state.get('rows') or [])}\n"


class TestTheSettingsDoor:
    async def test_host_is_refused_by_name(self, client):
        r = await client.post("/api/settings", json={"agent_name": "Synapse", "transform_runtime": "host"})
        assert r.status_code == 422, r.text
        assert "SYNAPSE_TRANSFORM_RUNTIME=host" in r.json()["detail"]

    async def test_the_sandbox_is_still_accepted(self, client):
        r = await client.post("/api/settings", json={"agent_name": "Synapse", "transform_runtime": "docker"})
        assert r.status_code == 200, r.text

    async def test_the_settings_say_what_the_server_runs(self, client, monkeypatch):
        monkeypatch.setenv("SYNAPSE_TRANSFORM_RUNTIME", "host")
        assert (await client.get("/api/settings")).json()["transform_runtime"] == "host"
        monkeypatch.delenv("SYNAPSE_TRANSFORM_RUNTIME")
        assert (await client.get("/api/settings")).json()["transform_runtime"] == "docker"


class TestTheImport:
    async def test_a_transform_in_a_bundle_is_named_in_its_warnings(self, client):
        bundle = {"synapse_export": "1.1", "orchestrations": [{
            "name": "Totals", "entry_step_id": "t", "state_schema": {},
            "steps": [{"id": "t", "name": "Add up", "type": "transform", "transform_code": CODE,
                       "output_key": "total", "next_step_id": None}]}]}
        r = await client.post("/api/import", json=everything(bundle))
        assert r.status_code == 200, r.text
        said = [w["message"] for w in r.json()["warnings"]]
        assert any("Add up" in m and "runs Python from this file" in m for m in said), said

    async def test_a_bundle_without_code_says_nothing_of_it(self, client):
        bundle = {"synapse_export": "1.1", "orchestrations": [{
            "name": "Hello", "entry_step_id": "p", "state_schema": {},
            "steps": [{"id": "p", "name": "Say", "type": "print", "print_content": "hi", "next_step_id": None}]}]}
        r = await client.post("/api/import", json=everything(bundle))
        assert not [w for w in r.json()["warnings"] if "runs Python" in w["message"]]
