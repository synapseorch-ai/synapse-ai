"""
v1 and v2 API contract fixes (F69).

* A misspelled or unknown agent fell back to a partial match and then to the
  oldest agent — on a fresh install, one of the AI builder's own agents. An
  unknown agent is now a 404, a lookup is an exact id or an exact name (any
  case), and the builder's agents are never reachable, listed or chosen.
* Sync resume reported a cancelled run as `completed`.
* `GET /api/v1/orchestrations/{id}` answered in a shape the definitions stopped
  having long ago (`label`, `depends_on`, `edges`); it returns the definition.
* A malformed image raised inside the handler (a 500); it is a 422.
* v2's `priority` was accepted and used by nothing; it is gone.
"""
import pytest

from _fakes import engine_events as E


@pytest.fixture
async def agents(seed_agent):
    from _fakes import seed as S
    mine = S.make_agent(id="agent_sales", name="Sales Bot")
    builder = S.make_agent(id="builder_planner", name="Builder Planner", system="native_builder")
    await S.seed_agents([builder, mine])      # the builder's agent is the oldest
    return {"mine": mine, "builder": builder}


@pytest.fixture
def final(monkeypatch):
    import core.react_engine as re
    seen = {}

    def loop(request, server, **kw):
        seen["agent_id"] = request.agent_id

        async def gen():
            yield E.final("ok")
        return gen()

    monkeypatch.setattr(re, "run_react_loop", loop)
    return seen


class TestV1AgentLookup:
    async def test_an_unknown_agent_is_not_found(self, client, api_key, agents, final):
        resp = await client.post("/api/v1/chat", json={"message": "hi", "agent": "Sales Bto"},
                                 headers=api_key["headers"])

        assert resp.status_code == 404
        assert "agent_id" not in final

    async def test_a_partial_name_is_not_a_match(self, client, api_key, agents, final):
        resp = await client.post("/api/v1/chat", json={"message": "hi", "agent": "Sales"},
                                 headers=api_key["headers"])

        assert resp.status_code == 404

    async def test_an_exact_name_in_any_case_is(self, client, api_key, agents, final):
        resp = await client.post("/api/v1/chat", json={"message": "hi", "agent": "sales bot"},
                                 headers=api_key["headers"])

        assert resp.status_code == 200
        assert final["agent_id"] == "agent_sales"

    async def test_no_agent_is_the_first_of_yours_not_the_builders(self, client, api_key, agents, final):
        resp = await client.post("/api/v1/chat", json={"message": "hi"}, headers=api_key["headers"])

        assert resp.status_code == 200
        assert final["agent_id"] == "agent_sales"

    async def test_the_builders_agents_cannot_be_named(self, client, api_key, agents, final):
        for ident in ("builder_planner", "Builder Planner"):
            resp = await client.post("/api/v1/chat", json={"message": "hi", "agent": ident},
                                     headers=api_key["headers"])
            assert resp.status_code == 404, ident

    async def test_nor_listed_or_read(self, client, api_key, agents):
        listed = (await client.get("/api/v1/agents", headers=api_key["headers"])).json()

        assert [a["id"] for a in listed] == ["agent_sales"]
        assert (await client.get("/api/v1/agents/builder_planner",
                                 headers=api_key["headers"])).status_code == 404

    async def test_the_stream_says_not_found_too(self, client, api_key, agents, final):
        resp = await client.post("/api/v1/chat/stream", json={"message": "hi", "agent": "nobody"},
                                 headers=api_key["headers"])

        assert resp.status_code == 404


class TestV1Images:
    async def test_a_malformed_image_is_a_422(self, client, api_key, agents, final):
        resp = await client.post("/api/v1/chat", json={"message": "hi", "images": ["not an image"]},
                                 headers=api_key["headers"])

        assert resp.status_code == 422


class TestV1Resume:
    async def test_a_cancelled_run_is_reported_cancelled(self, client, api_key, monkeypatch):
        import core.orchestration.engine as engine_mod
        monkeypatch.setattr(engine_mod, "OrchestrationEngine", E.fake_engine([
            E.orch_start(), {"type": "orchestration_complete", "status": "cancelled", "final_state": {}},
        ]))

        body = (await client.post("/api/v1/orchestrations/runs/run_x/resume",
                                  json={"response": "go"}, headers=api_key["headers"])).json()

        assert body["status"] == "cancelled"


class TestV1OrchestrationDefinition:
    async def test_it_is_the_definition(self, client, api_key, seed_orchestration):
        orch = await seed_orchestration(id="orch_def", entry_step_id="a", steps=[
            {"id": "a", "name": "First", "type": "print", "print_content": "x", "next_step_id": "b"},
            {"id": "b", "name": "Second", "type": "print", "print_content": "y", "next_step_id": None},
        ])

        body = (await client.get("/api/v1/orchestrations/orch_def", headers=api_key["headers"])).json()

        assert body["entry_step_id"] == "a"
        assert [(s["id"], s["name"], s["next_step_id"]) for s in body["steps"]] == [
            ("a", "First", "b"), ("b", "Second", None)]
        assert "edges" not in body


class TestV2:
    @pytest.fixture
    def v2(self, scale_app, monkeypatch):
        from _fakes.fake_pg import fake_session_factory
        scale_app.state.pg_session_factory = fake_session_factory()
        return scale_app

    async def test_an_unknown_agent_is_not_queued(self, client, v2, api_key, agents):
        resp = await client.post("/api/v2/chat", json={"message": "hi", "agent": "nobody"},
                                 headers=api_key["headers"])

        assert resp.status_code == 404
        v2.state.arq_redis.enqueue_job.assert_not_awaited()

    async def test_a_named_agent_is_queued_by_id(self, client, v2, api_key, agents):
        resp = await client.post("/api/v2/chat", json={"message": "hi", "agent": "Sales Bot"},
                                 headers=api_key["headers"])

        assert resp.status_code == 202
        assert v2.state.arq_redis.enqueue_job.call_args[1]["agent_id"] == "agent_sales"

    async def test_a_malformed_image_is_a_422(self, client, v2, api_key, agents):
        resp = await client.post("/api/v2/chat", json={"message": "hi", "images": ["nope"]},
                                 headers=api_key["headers"])

        assert resp.status_code == 422

    def test_priority_is_gone(self):
        from core.routes.api_v2 import V2ChatRequest, V2OrchestrationRunRequest
        assert "priority" not in V2ChatRequest.model_fields
        assert "priority" not in V2OrchestrationRunRequest.model_fields
