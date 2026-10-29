"""
An agent's knowledge bases save (B1b, D161).

`Agent.knowledge` names the knowledge bases an agent answers from: the engine
offers it `search_knowledge` / `read_knowledge` over them. `POST /api/agents`
validates against the `Agent` model, which drops what it does not declare
(F265's lesson), so without the field the agent editor's picker would reset on
every save. At most `MAX_KBS_PER_SEARCH` bases: one search covers all of them.
"""
import pytest

from _fakes import seed as S
from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from core.knowledge.api_models import MAX_KBS_PER_SEARCH
from core.models import AGENT_KNOWLEDGE_MAX

pytestmark = pytest.mark.usefixtures("knowledge_store")


@pytest.fixture
async def bases(client):
    """Two bases here, with Knowledge on: what saving an agent's bases is about. Saving one while Knowledge is off,
    or one that is not here, is refused (F1223), pinned in test_a_plans_agent_has_the_editors_switches."""
    assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200
    made = [(await client.post("/api/knowledge/bases", json={"name": n, "folder": n})).json()["id"]
            for n in ("help", "billing")]
    return made


async def _save(client, **fields):
    body = {**S.make_agent(id="agent_b1b_kb"), **fields}
    return await client.post("/api/agents", json=body)


async def _stored(client):
    return {a["id"]: a for a in (await client.get("/api/agents")).json()}["agent_b1b_kb"]


class TestAnAgentsKnowledgeBases:
    async def test_they_are_kept_and_answered(self, client, bases):
        a, b = bases
        resp = await _save(client, knowledge=[a, b])
        assert resp.status_code == 200, resp.text
        assert resp.json()["knowledge"] == [a, b], "the editor reloads the answer"
        assert (await _stored(client))["knowledge"] == [a, b]

    async def test_repeats_and_blanks_are_dropped_in_order(self, client, bases):
        a, b = bases
        resp = await _save(client, knowledge=[f" {b} ", a, b, ""])
        assert resp.status_code == 200, resp.text
        assert (await _stored(client))["knowledge"] == [b, a]

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
