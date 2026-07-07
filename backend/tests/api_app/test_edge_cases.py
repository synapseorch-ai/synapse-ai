"""
Adversarial / edge-case coverage: revoked & malformed API keys, vault path
traversal, oversized image lists, malformed LLM output, and orchestration global
guards (turn limit, bad parallel branch).
"""
import types

import pytest

from _fakes import engine_events as E, seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


class TestApiKeyEdges:
    async def test_revoked_key_is_rejected(self, client, seed_agent):
        seed_agent()
        import core.api_keys as ak
        raw, rec = ak.generate_api_key("to-revoke")
        keys = ak._load_keys()
        for k in keys:
            k["is_active"] = False
        ak._save_keys(keys)
        resp = await client.post("/api/v1/chat", json={"message": "hi"},
                                 headers={"Authorization": f"Bearer {raw}"})
        assert resp.status_code == 401

    async def test_malformed_bearer_scheme(self, client, seed_agent):
        seed_agent()
        resp = await client.post("/api/v1/chat", json={"message": "hi"},
                                 headers={"Authorization": "Token abc"})
        assert resp.status_code in (401, 403)

    async def test_wrong_prefix_key(self, client, seed_agent):
        seed_agent()
        resp = await client.post("/api/v1/chat", json={"message": "hi"},
                                 headers={"Authorization": "Bearer totally-bogus"})
        assert resp.status_code == 401


class TestVaultTraversal:
    def test_expand_mentions_blocks_traversal(self):
        from core.vault import expand_vault_mentions
        msg = "read @[../../../../etc/passwd]"
        # Traversal is rejected -> the mention is left untouched (no file content inlined).
        assert expand_vault_mentions(msg) == msg

    async def test_vault_file_traversal_is_handled(self, client):
        resp = await client.get("/api/vault/file", params={"path": "../../../../etc/passwd"})
        assert resp.status_code < 500  # rejected/404, never a 500 leak


class TestOversizedInputs:
    async def test_more_than_five_images_does_not_crash(self, client, monkeypatch):
        import core.routes.chat as chat
        monkeypatch.setattr(chat, "run_react_loop", E.gen_from([E.final("ok")]))
        resp = await client.post("/chat", json={"message": "hi", "images": ["data:x"] * 8})
        assert resp.status_code == 200


class TestMalformedLLMOutput:
    async def test_non_tool_json_treated_as_final(self, fake_llm):
        from core.react_engine import run_agent_step
        agent = S.make_agent(tools=[], skip_default_tools=True)
        fake_llm.script(['{"result": 5, "status": "ok"}'])  # valid JSON, not a tool call
        events = [ev async for ev in run_agent_step(
            message="go", agent_id=agent["id"], session_id="s1", server_module=_server(),
            agent_override=agent, tools_override=[], max_turns=2)]
        finals = [e for e in events if e.get("type") == "final"]
        assert finals and "result" in finals[-1]["response"]


class TestOrchestrationGuards:
    async def test_global_turn_limit(self, fake_llm):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        orch = S.make_orchestration(
            id="turnlimit", entry_step_id="p1", max_total_turns=1,
            steps=[
                {"id": "p1", "name": "One", "type": "print", "print_content": "1",
                 "output_key": "a", "next_step_id": "p2"},
                {"id": "p2", "name": "Two", "type": "print", "print_content": "2",
                 "output_key": "b", "next_step_id": None},
            ],
        )
        events = [ev async for ev in OrchestrationEngine(
            Orchestration.model_validate(orch), _server()).run("go", run_id="run_turn")]
        assert any(e.get("type") == "orchestration_error" and "turn limit" in e.get("error", "").lower()
                   for e in events)

    async def test_bad_parallel_branch_is_handled(self, fake_llm):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        orch = S.make_orchestration(
            id="badpar", entry_step_id="par",
            steps=[
                {"id": "par", "name": "Fan", "type": "parallel",
                 "parallel_branches": [["ghost_step"]], "next_step_id": None},
            ],
        )
        # A branch referencing a non-existent step must not crash the whole process.
        events = [ev async for ev in OrchestrationEngine(
            Orchestration.model_validate(orch), _server()).run("go", run_id="run_badpar")]
        assert any(e.get("type") in ("orchestration_error", "step_error", "step_warning",
                                     "orchestration_complete") for e in events)
