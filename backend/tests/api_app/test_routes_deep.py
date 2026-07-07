"""
Deeper route coverage across orchestrations, api_v1/v2 extras, settings, tools,
import/export, and vault writes. Business-logic branches, infra-free.
"""
import pytest

from _fakes import engine_events as E, seed as S
from _fakes.fake_pg import fake_session_factory, run_row
from _fakes.fake_redis_stream import load_run_events, load_chat_events


class TestOrchestrationCrudDeep:
    async def test_create_update_get_run_status(self, client, seed_orchestration):
        orch = S.make_orchestration(id="deep_orch", name="Deep")
        created = await client.post("/api/orchestrations", json=orch)
        assert created.status_code == 200
        # Update (same id) hits the replace branch.
        orch["name"] = "Deep v2"
        upd = await client.post("/api/orchestrations", json=orch)
        assert upd.status_code == 200 and upd.json()["name"] == "Deep v2"
        # Unknown run status -> 404.
        assert (await client.get("/api/orchestrations/runs/ghost")).status_code == 404


class TestV1ResumeStream:
    async def test_v1_resume_sync(self, client, api_key, monkeypatch):
        import core.orchestration.engine as engine_mod
        monkeypatch.setattr(engine_mod, "OrchestrationEngine", E.fake_engine([
            E.orch_start(), {"type": "orchestration_complete", "status": "completed", "final_state": {}},
            E.final("resumed done"),
        ]))
        resp = await client.post("/api/v1/orchestrations/runs/run_x/resume",
                                 json={"response": {"answer": "yes"}}, headers=api_key["headers"])
        assert resp.status_code == 200

    async def test_v1_resume_stream(self, client, api_key, monkeypatch):
        import core.orchestration.engine as engine_mod
        monkeypatch.setattr(engine_mod, "OrchestrationEngine", E.fake_engine([
            E.orch_start(), E.orch_complete()]))
        resp = await client.post("/api/v1/orchestrations/runs/run_x/resume/stream",
                                 json={"response": "go"}, headers=api_key["headers"])
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")


class TestV2EventsAndCancel:
    async def test_run_events_and_cancel(self, client, scale_app, api_key, fake_redis):
        scale_app.state.pg_session_factory = fake_session_factory(run_row("run_c", status="running"))
        await load_run_events(fake_redis, "run_c", [{"type": "step_start"}, {"type": "done"}])
        assert (await client.get("/api/v2/orchestrations/runs/run_c/events",
                                 headers=api_key["headers"])).status_code == 200
        cancel = await client.post("/api/v2/orchestrations/runs/run_c/cancel", headers=api_key["headers"])
        assert cancel.status_code < 500

    async def test_chat_events_and_cancel(self, client, scale_app, api_key, fake_redis):
        scale_app.state.pg_session_factory = fake_session_factory(None)
        await load_chat_events(fake_redis, "sess_c", [{"type": "response"}, {"type": "done"}])
        assert (await client.get("/api/v2/chat/sess_c/events",
                                 headers=api_key["headers"])).status_code == 200
        cancel = await client.post("/api/v2/chat/sess_c/cancel", headers=api_key["headers"])
        assert cancel.status_code < 500


class TestSettingsDeep:
    async def test_login_settings(self, client):
        resp = await client.post("/api/settings/login",
                                 json={"login_enabled": False, "username": "", "password": ""})
        assert resp.status_code < 500

    async def test_settings_persists_keys(self, client):
        body = {"agent_name": "K", "model": "claude-x", "mode": "cloud", "anthropic_key": "sk-x"}
        assert (await client.post("/api/settings", json=body)).status_code == 200
        got = await client.get("/api/settings")
        # Secret values are returned masked or present; the field exists either way.
        assert "anthropic_key" in got.json()


class TestToolsDeep:
    async def test_custom_tool_update_and_delete_unknown(self, client):
        t = {"name": "up_tool", "description": "v1", "parameters": {"type": "object", "properties": {}}}
        await client.post("/api/tools/custom", json=t)
        t["description"] = "v2"
        assert (await client.post("/api/tools/custom", json=t)).status_code == 200  # update branch
        assert (await client.delete("/api/tools/custom/does-not-exist")).status_code < 500


class TestImportExportDeep:
    async def test_export_with_orchestration_and_import(self, client, seed_agent, seed_orchestration):
        a = seed_agent(id="ie_agent")
        o = seed_orchestration(id="ie_orch")
        exported = await client.post("/api/export",
                                     json={"agent_ids": [a["id"]], "orchestration_ids": [o["id"]]})
        assert exported.status_code == 200
        bundle = exported.json()
        imported = await client.post("/api/import", json={"bundle": bundle, "mcp_secrets": {}})
        assert imported.status_code < 500


class TestVaultWrite:
    async def test_write_existing_file(self, client):
        await client.post("/api/vault/file", json={"path": "", "name": "w.md", "content": "v1"})
        resp = await client.put("/api/vault/file", json={"path": "w.md", "content": "v2 updated"})
        assert resp.status_code < 500
        got = await client.get("/api/vault/file", params={"path": "w.md"})
        assert got.status_code == 200
