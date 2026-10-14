"""An agent saved through its editor or the API is told which of its tools nothing provides (F526, VOX1f, D273).

Harbor, VOX1b's rig agent, was saved by a script with `web_search`, which named no tool in its workspace; the engine
dropped it from the model's list without a word, so two sessions' weather asks transferred and one read it as "a
vague ask". The builder refuses such a name (F489); a save here is told instead, and the agent's page asks when it
opens, since an agent saved by a script never went through the editor.
"""
from test_agents_routes import _valid_agent_body


async def test_a_save_names_the_tool_nothing_provides_and_keeps_the_agent_as_sent(client):
    r = await client.post("/api/agents", json=_valid_agent_body(id="agent_f526", tools=["list_agentz", "all"]))
    assert r.status_code == 200, r.text
    [unknown] = r.json()["unknown_tools"]
    assert unknown["name"] == "list_agentz" and "list_agents" in unknown["nearest"]
    stored = next(a for a in (await client.get("/api/agents")).json() if a["id"] == "agent_f526")
    assert stored["tools"] == ["list_agentz", "all"], "saved as given: not refused"
    assert "unknown_tools" not in stored, "an answer, never stored"


async def test_real_tools_say_nothing(client):
    r = await client.post("/api/agents", json=_valid_agent_body(id="agent_f526b", tools=["list_agents"]))
    assert r.json()["unknown_tools"] == []


async def test_the_page_asks_about_a_list(client):
    r = await client.post("/api/agents/tool-check", json={"tools": ["web_search_xyz", "list_agents", "group:g1"]})
    assert r.status_code == 200
    assert [t["name"] for t in r.json()["unknown_tools"]] == ["web_search_xyz"]
