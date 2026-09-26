"""An agent's name is at most 100 characters, on every path that writes one (F217).

The OSS test pass saved a 203-character agent name: no path had a limit. The
header and switcher truncated it, but the chat's empty state spelled it out in
letter-spaced capitals, 1,742 px wide, off both edges of the page. The screens
now truncate and wrap regardless; this is the other half (Naveen, 2026-09-26):
100 characters, refused where a person or the builder chose the name, clipped
where a model generated it in one go. Agents already stored with longer names
still load — the limit is on writing, not reading.
"""
from __future__ import annotations

import json
import types

import pytest

from _fakes import seed as S
from core.builder_tools import execute_builder_tool
from core.models import AGENT_NAME_MAX

LONG = "N" * (AGENT_NAME_MAX + 1)
EXACT = "E" * AGENT_NAME_MAX


def _agent(name: str, **extra) -> dict:
    return {"id": extra.pop("id", "agent_named"), "name": name, "description": "d", "tools": [],
            "system_prompt": "p", **extra}


async def _builder(tool: str, args: dict) -> dict:
    server = types.SimpleNamespace(agent_sessions={})
    return json.loads(await execute_builder_tool(tool, args, server))


async def _names() -> list[str]:
    from core.store.resources import load_agents
    return [a["name"] for a in await load_agents()]


class TestTheAppRoute:
    async def test_a_name_past_the_limit_is_refused(self, client):
        response = await client.post("/api/agents", json=_agent(LONG))
        assert response.status_code == 422, response.text
        assert LONG not in await _names()

    async def test_a_name_at_the_limit_is_saved(self, client):
        response = await client.post("/api/agents", json=_agent(EXACT))
        assert response.status_code == 200, response.text
        assert EXACT in await _names()


class TestTheBuilder:
    async def test_create_agent_refuses_it(self):
        result = await _builder("create_agent", {"name": LONG, "description": "d", "type": "conversational",
                                                 "tools": [], "system_prompt": "p"})
        assert "error" in result and str(AGENT_NAME_MAX) in result["error"], result
        assert LONG not in await _names()

    async def test_create_agents_refuses_only_that_one(self):
        result = await _builder("create_agents", {"agents": [
            {"role": "long", "name": LONG, "description": "d", "type": "conversational", "tools": [], "system_prompt": "p"},
            {"role": "fine", "name": "Fine", "description": "d", "type": "conversational", "tools": [], "system_prompt": "p"},
        ]})
        assert [c["name"] for c in result["agents"]] == ["Fine"]
        assert [e["role"] for e in result["errors"]] == ["long"]
        assert str(AGENT_NAME_MAX) in result["errors"][0]["error"]
        assert LONG not in await _names()

    async def test_update_agent_refuses_it(self):
        await S.seed_agents([S.make_agent(id="agent_keep", name="Kept")])
        result = await _builder("update_agent", {"agent_id": "agent_keep", "fields": {"name": LONG}})
        assert "error" in result and str(AGENT_NAME_MAX) in result["error"], result
        assert "Kept" in await _names()


class TestAModelWrittenName:
    async def test_is_clipped_rather_than_failing_the_build(self, client, monkeypatch):
        import core.routes.agents as agents_mod

        async def generated(*_a, **_kw):
            return json.dumps({"name": LONG, "description": "d", "system_prompt": "p", "tools": []})

        monkeypatch.setattr(agents_mod, "llm_generate_response", generated)
        response = await client.post("/api/agents/build", json={"description": "an agent"})
        assert response.status_code == 200, response.text
        saved = [n for n in await _names() if n.startswith("N")]
        assert saved and all(len(n) <= AGENT_NAME_MAX for n in saved), saved


class TestAnAgentAlreadyStored:
    async def test_still_loads(self, client):
        await S.seed_agents([S.make_agent(id="agent_old_long", name="O" * 203)])
        listed = (await client.get("/api/agents")).json()
        assert any(a["id"] == "agent_old_long" for a in listed)
        assert "agent_old_long" in (await client.get("/api/status")).json()["agents"]
