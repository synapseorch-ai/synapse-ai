"""An agent or tool saved from a stale editor is refused, not written over a newer save; a deleted one is said deleted
(F891, F871's pattern for the other two whole-document saves; Naveen 2026-10-23).

The editor says what it read with `If-Match: "<updated_at>"` — the row's own stamp, which the list and every save now
carry (an agent's or tool's document has none, and nothing wrote one). Stale: 409 `{"message", "updated_at"}`; gone:
404; no header (the builder, an import, every older caller): unchanged. A tool is keyed by its name, so the editor
sends the header only while the name is the one it opened: a rename is a new tool, as before.
"""
import asyncio

from _fakes import seed as S


def _seen(doc: dict) -> dict:
    return {"If-Match": f'"{doc["updated_at"]}"'}


async def _listed(client, path: str, key: str, value: str) -> dict:
    return next(x for x in (await client.get(path)).json() if x.get(key) == value)


class TestAnAgent:
    async def _agent(self, client) -> dict:
        resp = await client.post("/api/agents", json=S.make_agent(id="agent_f891", name="Dispatch"))
        assert resp.status_code == 200
        return resp.json()

    async def test_the_list_and_a_save_carry_the_rows_stamp(self, client):
        saved = await self._agent(client)
        assert saved["updated_at"]
        assert (await _listed(client, "/api/agents", "id", "agent_f891"))["updated_at"] == saved["updated_at"]

    async def test_a_save_from_a_stale_copy_is_refused_and_writes_nothing(self, client):
        a = await self._agent(client)
        await asyncio.sleep(0.01)
        newer = (await client.post("/api/agents", json={**a, "name": "A's"}, headers=_seen(a))).json()
        resp = await client.post("/api/agents", json={**a, "name": "B's"}, headers=_seen(a))
        assert resp.status_code == 409
        assert resp.json()["detail"]["updated_at"] == newer["updated_at"]
        assert "agent was changed elsewhere" in resp.json()["detail"]["message"]
        assert (await _listed(client, "/api/agents", "id", "agent_f891"))["name"] == "A's"
        kept = await client.post("/api/agents", json={**a, "name": "B's"},
                                 headers={"If-Match": f'"{newer["updated_at"]}"'})
        assert kept.status_code == 200 and kept.json()["name"] == "B's"

    async def test_a_builder_write_in_between_makes_the_copy_stale(self, client):
        from core.builder_tools import execute_builder_tool

        a = await self._agent(client)
        await asyncio.sleep(0.01)

        class _Server:
            agent_sessions: dict = {}

        await execute_builder_tool("update_agent", {"agent_id": "agent_f891", "description": "Builder's"}, _Server())
        resp = await client.post("/api/agents", json={**a, "name": "Mine"}, headers=_seen(a))
        assert resp.status_code == 409

    async def test_a_deleted_agent_is_404_and_not_brought_back(self, client):
        a = await self._agent(client)
        assert (await client.delete("/api/agents/agent_f891")).status_code == 200
        resp = await client.post("/api/agents", json=a, headers=_seen(a))
        assert resp.status_code == 404 and "deleted" in resp.json()["detail"]["message"]
        assert not [x for x in (await client.get("/api/agents")).json() if x.get("id") == "agent_f891"]

    async def test_no_if_match_is_unchanged(self, client):
        a = await self._agent(client)
        await asyncio.sleep(0.01)
        await client.post("/api/agents", json={**a, "name": "A's"}, headers=_seen(a))
        assert (await client.post("/api/agents", json={**a, "name": "Old caller"})).status_code == 200


TOOL = {"name": "rotterdam_weather", "description": "Weather at Rotterdam.", "method": "GET",
        "url": "https://example.test/weather", "tool_type": "http", "parameters": []}


class TestATool:
    async def _tool(self, client) -> dict:
        assert (await client.post("/api/tools/custom", json=TOOL)).status_code == 200
        return await _listed(client, "/api/tools/custom", "name", TOOL["name"])

    async def test_a_save_carries_the_rows_stamp_and_never_stores_it(self, client):
        listed = await self._tool(client)
        assert listed["updated_at"]
        resp = await client.post("/api/tools/custom", json={**listed, "description": "v2"}, headers=_seen(listed))
        assert resp.status_code == 200 and resp.json()["tool"]["updated_at"] != listed["updated_at"]
        from core.store.resources import load_tools
        stored = next(t for t in await load_tools() if t.get("name") == TOOL["name"])
        assert "updated_at" not in stored, "the stamp is the row's, never the document's"

    async def test_a_save_from_a_stale_copy_is_refused(self, client):
        t = await self._tool(client)
        await asyncio.sleep(0.01)
        await client.post("/api/tools/custom", json={**t, "description": "A's"}, headers=_seen(t))
        resp = await client.post("/api/tools/custom", json={**t, "description": "B's"}, headers=_seen(t))
        assert resp.status_code == 409 and "tool was changed elsewhere" in resp.json()["detail"]["message"]
        assert (await _listed(client, "/api/tools/custom", "name", TOOL["name"]))["description"] == "A's"

    async def test_a_deleted_tool_is_404(self, client):
        t = await self._tool(client)
        from core.store.resources import delete_tool_named
        await delete_tool_named(TOOL["name"])
        resp = await client.post("/api/tools/custom", json=t, headers=_seen(t))
        assert resp.status_code == 404

    async def test_no_if_match_is_unchanged(self, client):
        t = await self._tool(client)
        await asyncio.sleep(0.01)
        await client.post("/api/tools/custom", json={**t, "description": "A's"}, headers=_seen(t))
        assert (await client.post("/api/tools/custom", json={**t, "description": "Old caller"})).status_code == 200
