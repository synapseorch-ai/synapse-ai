"""An agent's memory can be seen, corrected and removed by a person (F270, F297).

The memory engine runs in OSS — `memory/<agent>/` holds the index, the saved
memories and the session traces — but nothing in the product showed it. A user
could not see, correct or delete what an agent remembered except by asking the
agent to `forget_memory`, and Settings → Memory's "clear data" had no category
for it. Deleting the agent left it behind, so a new agent given the same id
inherited what the old one remembered (F297).

Now the agent editor reads it (cloud's viewer, with edit and delete), clear data
wipes it, and deleting an agent deletes its memory. These are owner routes: a
widget visitor's token reaches none of them (F276 keeps visitors out of memory
altogether).
"""
import pytest

from _fakes import seed as S
from core import agent_memory as am

AGENT = "agent_f270"


@pytest.fixture(autouse=True)
def _fresh_index():
    am.set_memory_index(None)
    yield
    am.set_memory_index(None)


@pytest.fixture
async def remembered():
    await S.seed_agents([S.make_agent(id=AGENT, name="Keeper", memory=True)])
    await am.write_memory(AGENT, "dock-code", "The loading bay code is 4471.")
    await am.write_memory(AGENT, "manager", "The Osaka manager is Aiko Tanaka.")
    am.append_session_note(AGENT, "s_one", am.turn_trace("what is the code?", "4471"))
    return AGENT


class TestSeeingIt:
    async def test_the_index_the_memories_and_the_sessions(self, client, remembered):
        resp = await client.get(f"/api/agents/{AGENT}/memory")
        assert resp.status_code == 200, resp.text
        view = resp.json()
        assert view["enabled"] is True
        assert "[dock-code]" in view["index"] and view["index_lines"] == 2
        assert [i["name"] for i in view["items"]] == ["manager", "dock-code"], "newest first, as list_memories"
        assert view["items"][0]["summary"].startswith("The Osaka manager")
        assert [s["name"] for s in view["sessions"]] == ["s_one"]
        assert view["sessions"][0]["size"] > 0

    async def test_one_file_whole(self, client, remembered):
        item = (await client.get(f"/api/agents/{AGENT}/memory/items/dock-code")).json()
        assert item["content"] == "The loading bay code is 4471."
        session = (await client.get(f"/api/agents/{AGENT}/memory/sessions/s_one")).json()
        assert "what is the code?" in session["content"]

    async def test_what_is_not_there_is_a_404(self, client, remembered):
        assert (await client.get(f"/api/agents/{AGENT}/memory/items/nope")).status_code == 404
        assert (await client.get(f"/api/agents/{AGENT}/memory/archive/x")).status_code == 404
        assert (await client.get("/api/agents/agent_none/memory")).status_code == 404

    async def test_a_name_cannot_leave_the_agents_folder(self, client, remembered):
        resp = await client.get(f"/api/agents/{AGENT}/memory/items/..%2F..%2Fnotifications.json")
        assert resp.status_code == 404


class TestCorrectingIt:
    async def test_an_edit_is_saved_and_indexed_as_the_newest(self, client, remembered):
        resp = await client.put(f"/api/agents/{AGENT}/memory/items/dock-code",
                                json={"content": "The loading bay code is 5582 (changed in May)."})
        assert resp.status_code == 200, resp.text
        assert am.read_memory(AGENT, "dock-code").startswith("The loading bay code is 5582")
        view = (await client.get(f"/api/agents/{AGENT}/memory")).json()
        assert view["items"][0]["name"] == "dock-code"
        assert "5582" in am.read_index(AGENT) and "4471" not in am.read_index(AGENT)

    async def test_an_empty_edit_is_refused(self, client, remembered):
        resp = await client.put(f"/api/agents/{AGENT}/memory/items/dock-code", json={"content": "  "})
        assert resp.status_code == 422
        assert am.read_memory(AGENT, "dock-code") == "The loading bay code is 4471."


class TestRemovingIt:
    async def test_one_memory(self, client, remembered):
        assert (await client.delete(f"/api/agents/{AGENT}/memory/items/dock-code")).status_code == 200
        assert am.read_memory(AGENT, "dock-code") is None
        assert "dock-code" not in am.read_index(AGENT)
        assert (await client.delete(f"/api/agents/{AGENT}/memory/items/dock-code")).status_code == 404

    async def test_one_whose_line_was_archived_leaves_no_line_behind(self, client, remembered):
        for i in range(am.MAX_INDEX_LINES + 1):
            await am.write_memory(AGENT, f"fact-{i:03d}", f"fact number {i}")
        assert "[dock-code]" in (am._store().get(am._archive_key(AGENT)) or "")
        await client.delete(f"/api/agents/{AGENT}/memory/items/dock-code")
        assert "[dock-code]" not in (am._store().get(am._archive_key(AGENT)) or "")
        assert "dock-code" not in am.list_memories(AGENT, "items", page=3)

    async def test_one_session(self, client, remembered):
        assert (await client.delete(f"/api/agents/{AGENT}/memory/sessions/s_one")).status_code == 200
        assert am.read_session(AGENT, "s_one") is None

    async def test_everything_it_remembered(self, client, remembered):
        resp = await client.delete(f"/api/agents/{AGENT}/memory")
        assert resp.status_code == 200 and resp.json()["deleted"] >= 4
        view = (await client.get(f"/api/agents/{AGENT}/memory")).json()
        assert view["index"] == "" and view["items"] == [] and view["sessions"] == []
        assert await am.search_memory(AGENT, "loading bay code") == []

    async def test_clear_data_wipes_every_agents_memory(self, client, remembered):
        await S.seed_agents([S.make_agent(id=AGENT, memory=True), S.make_agent(id="agent_f270_b", memory=True)])
        await am.write_memory("agent_f270_b", "x", "other agent's note")
        resp = await client.post("/api/memory/clear", json={"items": ["agent_memory"]})
        assert resp.status_code == 200, resp.text
        assert "agent_memory" in resp.json()["cleared"]
        assert am.read_index(AGENT) == "" and am.read_index("agent_f270_b") == ""
        assert am._store().list("memory/") == []

    async def test_other_clear_data_leaves_memory_alone(self, client, remembered):
        await client.post("/api/memory/clear", json={"items": ["chat_history"]})
        assert am.read_memory(AGENT, "dock-code") is not None


class TestDeletingTheAgent:
    async def test_its_memory_goes_with_it(self, client, remembered):
        """F297: an agent created later with the same id started out knowing
        what the deleted one had remembered."""
        assert (await client.delete(f"/api/agents/{AGENT}")).status_code == 200
        assert am.read_index(AGENT) == ""
        assert am._store().list(am._agent_prefix(AGENT) + "/") == []

    async def test_another_agents_memory_is_untouched(self, client, remembered):
        await S.seed_agents([S.make_agent(id=AGENT, memory=True), S.make_agent(id="agent_f270_keep", memory=True)])
        await am.write_memory("agent_f270_keep", "k", "keep me")
        await client.delete(f"/api/agents/{AGENT}")
        assert am.read_memory("agent_f270_keep", "k") == "keep me"


class TestNeverAVisitors:
    def test_no_widget_route_touches_memory(self, test_app):
        # The schema, not `app.routes`: on this FastAPI that holds routers, and
        # a walk of it finds nothing to refuse (cloud's F166).
        paths = list(test_app.openapi()["paths"])
        visitor = [p for p in paths if p.startswith("/api/widget/")]
        assert len(visitor) >= 7, "the walk found the widget's routes"
        assert not [p for p in visitor if "memory" in p]
        assert "/api/agents/{agent_id}/memory" in paths

    def test_the_memory_routes_need_a_members_session(self):
        from core import internal_auth
        assert not any("/api/agents".startswith(p) or p.startswith("/api/agents")
                       for p in internal_auth._SESSIONLESS_PREFIXES)
