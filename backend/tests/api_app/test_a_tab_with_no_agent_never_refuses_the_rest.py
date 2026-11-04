"""One tab the status read can't take never refuses the others (F1336, REACH1; REL3's fresh 2.0 install, eb 11-04).

The chat tab strip asks how every open tab stands in one request (`POST /api/sessions/status`, TABS1). A tab opened
before an agent was picked has no agent yet, and its empty `agent_id` failed the whole request with a 422: every tab
lost its status, and the console said so on every poll. These pin the class:

- each tab in the request is read on its own: one the read can't take is answered with why, and every other tab with
  its status; only a request of more tabs than one person keeps open is refused whole;
- the page asks only for tabs that have both a conversation and an agent, so a tab with no agent asks nothing.
"""
from __future__ import annotations

from pathlib import Path


async def _status(client, *items: dict) -> list[dict]:
    response = await client.post("/api/sessions/status", json={"sessions": list(items)})
    assert response.status_code == 200, response.text
    return response.json()["sessions"]


class TestEachTabIsReadOnItsOwn:
    async def test_a_tab_with_no_agent_is_told_why_and_the_rest_are_answered(self, client):
        rows = await _status(client, {"session_id": "tab-ok", "agent_id": "agent_a"},
                             {"session_id": "tab-new", "agent_id": ""})
        by_id = {row["session_id"]: row for row in rows}
        assert by_id["tab-ok"]["turn_count"] == 0 and "error" not in by_id["tab-ok"], by_id
        assert by_id["tab-new"]["error"] and "turn_count" not in by_id["tab-new"], by_id

    async def test_any_entry_it_cannot_read_is_its_own_answer(self, client):
        rows = await _status(client, {"session_id": "tab-ok", "agent_id": "agent_a"}, {"session_id": "tab-x"},
                             {"agent_id": "agent_a"}, "not a tab", {"session_id": "", "agent_id": "agent_a"})
        assert [row.get("error") is None for row in rows] == [True, False, False, False, False], rows

    async def test_more_tabs_than_one_person_keeps_is_still_refused_whole(self, client):
        response = await client.post("/api/sessions/status", json={
            "sessions": [{"session_id": f"s{i}", "agent_id": "agent_a"} for i in range(11)]})
        assert response.status_code == 422


class TestThePageAsksOnlyForTabsWithAnAgent:
    PAGE = Path(__file__).resolve().parents[3] / "frontend/src/app/(app)/page.tsx"

    def test_read_tab_status_leaves_out_a_tab_with_no_agent(self):
        page = self.PAGE.read_text()
        read = page[page.index("async function readTabStatus"):]
        read = read[:read.index("\n}\n")]
        asked = read[:read.index("fetch(")]
        assert ".filter(" in asked and "agentId" in asked and "sessionId" in asked, asked
        assert "tabs.map(" not in read.split("JSON.stringify", 1)[1].split(")", 1)[0], "it maps the asked tabs only"
