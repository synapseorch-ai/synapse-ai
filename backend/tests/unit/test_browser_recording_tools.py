"""Recordings as an agent's tool, and an agent's fix of a missed step (BRW1, D328, F1144/F1145; DESIGN §v5.3–5.4):
only a picked recording runs, whatever the model asks; a fix is only ever proposed, never applied."""
from __future__ import annotations

from core.browser import client as browser_client
from core.browser import recordings

ORDER = {"role": "textbox", "name": "Order number", "label": "Order number", "css": "#order"}


def _steps() -> list[dict]:
    return [{"id": "s1", "action": "open", "url": "https://portal.example.com/"},
            {"id": "s2", "action": "type", "target": ORDER, "value": {"input": "order number"}},
            {"id": "s3", "action": "click", "target": {"role": "button", "name": "Search"}}]


class _Host:
    """The component, answering a replay as told; records what it was asked."""

    running = True
    proc = object()

    def __init__(self, answers: dict | None = None):
        self.calls: list[tuple[str, dict | None]] = []
        self.answers = answers or {}

    async def ensure(self):
        pass

    async def viewer(self, path, *, params=None, json=None, timeout=60):
        return 200, {}

    async def call(self, method, path, *, json=None, params=None, timeout=120):
        self.calls.append((path, json))
        if path == "/session":
            return 200, {"session_id": "s1"}
        return 200, self.answers.get(path, {"done": 3, "outputs": {}})


async def _run(tools, owner, args, picks):
    tokens = (browser_client.owner.set(owner), browser_client.settings.set({"recordings": picks}))
    try:
        return await tools.call_tool("browser_run_recording", args)
    finally:
        browser_client.owner.reset(tokens[0])
        browser_client.settings.reset(tokens[1])


async def test_only_a_recording_picked_for_the_agent_runs_whatever_it_asks():
    mine = await recordings.create("Find an order", _steps(), by="you")
    other = await recordings.create("Close my account", _steps(), by="you")
    host = _Host()
    tools = browser_client.BrowserTools(host)
    for asked in (other["id"], "close-my-account", "", "find-an-order-please"):
        out = await _run(tools, "chat:c1", {"recording": asked, "inputs": {"order number": "A-1"}}, [mine["id"]])
        assert out.isError and "not one of the recordings picked for you" in out.content[0].text, asked
    assert not [c for c in host.calls if c[0].startswith("/replay")], "nothing replayed for a recording not picked"
    ran = await _run(tools, "chat:c1", {"recording": "find-an-order", "inputs": {"order number": "A-1"}}, [mine["id"]])
    assert not ran.isError and host.calls[-1][0] == "/replay" and host.calls[-1][1]["recording_id"] == mine["id"]
    assert host.calls[-1][1]["inputs"] == {"order number": "A-1"} and host.calls[-1][1]["unattended"] is False


async def test_a_recordings_inputs_are_asked_for_before_it_runs_and_a_run_replays_unattended():
    mine = await recordings.create("Find an order", _steps(), by="you")
    host = _Host()
    tools = browser_client.BrowserTools(host)
    out = await _run(tools, "run:r1", {"recording": mine["id"]}, [mine["id"]])
    assert out.isError and "order number" in out.content[0].text
    await _run(tools, "run:r1", {"recording": mine["id"], "inputs": {"order number": "B"}}, [mine["id"]])
    assert host.calls[-1][1]["unattended"] is True, "a run's replay keeps sends to what the recording did"


async def test_an_agents_fix_is_a_pending_edit_and_never_changes_the_recording():
    mine = await recordings.create("Find an order", _steps(), by="you")
    fixed = [{"id": "f1", "action": "click", "actor": "agent", "target": {"role": "button", "name": "Find"},
              "value": None, "line": "Click Find"}]
    host = _Host({"/replay/continue": {"done": 3, "outputs": {"orders": []},
                                       "fix": {"step_id": "s3", "n": 3, "steps": fixed, "shots": {}}}})
    tools = browser_client.BrowserTools(host)
    out = await _run(tools, "run:r7", {"recording": mine["id"], "carry_on": True}, [mine["id"]])
    assert "proposed change" in out.content[0].text and not out.isError
    doc = await recordings.get(mine["id"])
    assert doc["version"] == 1 and [s["id"] for s in doc["steps"]] == ["s1", "s2", "s3"], "unchanged till approved"
    (edit,) = doc["pending_edits"]
    assert edit["step_id"] == "s3" and edit["by"] == "agent" and edit["run_id"] == "r7"
    assert [s["id"] for s in edit["steps"]] == ["f1"]


async def test_a_miss_tells_the_agent_the_step_and_the_page_and_how_to_carry_on():
    mine = await recordings.create("Find an order", _steps(), by="you")
    host = _Host({"/replay": {"miss": {"n": 3, "step_id": "s3", "line": "Click Search", "why": "its target is not "
                                       "on the page", "snapshot": "- button \"Find\" [ref=e7]"}, "done": 2}})
    tools = browser_client.BrowserTools(host)
    out = await _run(tools, "chat:c1", {"recording": mine["id"], "inputs": {"order number": "A"}}, [mine["id"]])
    text = out.content[0].text
    assert out.isError and "step 3" in text and "Click Search" in text and 'button "Find"' in text
    assert "carry_on" in text and "redone once" in text


def test_a_recording_is_named_by_its_words_and_lists_its_inputs():
    assert recordings.slug({"id": "rec_1", "name": "Find an Order (portal)"}) == "find-an-order-portal"
    assert recordings.slug({"id": "rec_1", "name": ""}) == "rec_1"
    assert recordings.input_names({"steps": _steps() + [
        {"action": "type", "value": {"input": "order number"}}, {"action": "type", "value": {"input": "date"}}]}) \
        == ["order number", "date"]


async def test_a_browser_agents_instructions_list_its_recordings():
    from core.react_engine import _inject_recordings
    mine = await recordings.create("Find an order", _steps(), by="you")
    agent = {"type": "browser", "browser": {"recordings": [mine["id"], "rec_gone"]}}
    text = await _inject_recordings(agent, "base")
    assert "`find-an-order`" in text and "order number" in text and "on example.com;" in text  # its sites, by domain
    assert "rec_gone" not in text
    assert await _inject_recordings({"type": "conversational", "browser": {"recordings": [mine["id"]]}}, "b") == "b"


async def test_an_agent_picks_only_recordings_that_exist_and_a_deleted_one_drops_out(client):
    from core.store.resources import get_agent
    mine = await recordings.create("Find an order", _steps(), by="you")
    body = {"id": "b10", "name": "B", "description": "d", "type": "browser", "tools": [], "system_prompt": "x",
            "browser": {"recordings": [mine["id"], "rec_nope"]}}
    assert (await client.post("/api/agents", json=body)).status_code == 422
    body["browser"]["recordings"] = [mine["id"]]
    assert (await client.post("/api/agents", json=body)).status_code == 200
    await recordings.delete(mine["id"])
    assert (await client.post("/api/agents", json=body)).status_code == 200, "a deleted pick never blocks a save"
    assert (await get_agent("b10"))["browser"]["recordings"] == []
