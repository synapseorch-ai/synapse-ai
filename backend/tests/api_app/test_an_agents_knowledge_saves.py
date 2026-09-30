"""
An agent's knowledge bases save (B1b, D161).

`Agent.knowledge` names the knowledge bases an agent answers from: the engine
offers it `search_knowledge` / `read_knowledge` over them. `POST /api/agents`
validates against the `Agent` model, which drops what it does not declare
(F265's lesson), so without the field the agent editor's picker would reset on
every save. At most `MAX_KBS_PER_SEARCH` bases: one search covers all of them.
"""
from _fakes import seed as S
from core.knowledge.api_models import MAX_KBS_PER_SEARCH
from core.models import AGENT_KNOWLEDGE_MAX


async def _save(client, **fields):
    body = {**S.make_agent(id="agent_b1b_kb"), **fields}
    return await client.post("/api/agents", json=body)


async def _stored(client):
    return {a["id"]: a for a in (await client.get("/api/agents")).json()}["agent_b1b_kb"]


class TestAnAgentsKnowledgeBases:
    async def test_they_are_kept_and_answered(self, client):
        resp = await _save(client, knowledge=["kb_00000000000a", "kb_00000000000b"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["knowledge"] == ["kb_00000000000a", "kb_00000000000b"], "the editor reloads the answer"
        assert (await _stored(client))["knowledge"] == ["kb_00000000000a", "kb_00000000000b"]

    async def test_repeats_and_blanks_are_dropped_in_order(self, client):
        resp = await _save(client, knowledge=[" kb_00000000000b ", "kb_00000000000a", "kb_00000000000b", ""])
        assert resp.status_code == 200, resp.text
        assert (await _stored(client))["knowledge"] == ["kb_00000000000b", "kb_00000000000a"]

    async def test_more_than_one_search_covers_is_a_422(self, client):
        too_many = [f"kb_{i:012x}" for i in range(AGENT_KNOWLEDGE_MAX + 1)]
        resp = await _save(client, knowledge=too_many)
        assert resp.status_code == 422, resp.text

    async def test_an_agent_saved_without_them_has_none(self, client):
        resp = await _save(client)
        assert resp.status_code == 200, resp.text
        assert (await _stored(client)).get("knowledge") == []

    def test_the_cap_is_one_search(self):
        assert AGENT_KNOWLEDGE_MAX == MAX_KBS_PER_SEARCH
