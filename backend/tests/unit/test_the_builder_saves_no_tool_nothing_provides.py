"""The AI builder saves no tool that nothing provides (F489, BWA1, D274).

SW1 met it in every Agent Builder translation, and VOX1e too: `create_agent` took any tool name and saved it, and the
agent then ran silently without it; a forced `tool` step naming one failed on its first run. Every name is now
checked with the lookup a run makes (`core.tools.resolve_one_tool`):

- creating or updating an agent with a tool nothing provides is refused, with the nearest names that exist;
  `create_agents` refuses that one spec and makes the others; `all` and `group:` entries are lists, not names;
- validating a workflow: a forced tool nothing provides is an issue (its first run would fail); an agent naming one
  is a warning (a workspace's older agent may name a tool from a server that has gone).
"""
from __future__ import annotations

import json

import pytest

from core.builder_tools import execute_builder_tool


class _Server:
    agent_sessions: dict = {}


async def _call(tool: str, args: dict) -> dict:
    return json.loads(await execute_builder_tool(tool, args, _Server()))


async def test_an_agent_with_a_tool_nothing_provides_is_refused_with_the_nearest_real_one():
    got = await _call("create_agent", {"name": "Reader", "description": "Reads.", "type": "conversational",
                                       "tools": ["list_agentz"], "system_prompt": "Read."})
    assert "No tool here is called 'list_agentz'" in got["error"] and "'list_agents'" in got["error"]


@pytest.mark.parametrize("tools", [["all"], ["list_agents", "get_agent"], ["group:g1"], []])
async def test_real_tools_and_lists_are_saved(tools):
    got = await _call("create_agent", {"name": "Reader", "description": "Reads.", "type": "conversational",
                                       "tools": tools, "system_prompt": "Read."})
    assert "error" not in got, got


async def test_create_agents_refuses_the_one_spec_and_makes_the_rest():
    got = await _call("create_agents", {"agents": [
        {"role": "good", "name": "Lister", "description": "d", "type": "conversational", "tools": ["list_agents"],
         "system_prompt": "s"},
        {"role": "bad", "name": "Searcher", "description": "d", "type": "conversational", "tools": ["file_search"],
         "system_prompt": "s"},
    ]})
    assert [c["name"] for c in got.get("agents", [])] == ["Lister"], got
    assert any(e.get("role") == "bad" and "file_search" in e["error"] for e in got.get("errors", [])), got


async def test_updating_an_agents_tools_to_a_phantom_is_refused():
    made = await _call("create_agent", {"name": "Reader", "description": "Reads.", "type": "conversational",
                                        "tools": ["list_agents"], "system_prompt": "Read."})
    agent_id = made.get("id") or made.get("agent", {}).get("id")
    got = await _call("update_agent", {"agent_id": agent_id, "fields": {"tools": ["web_search"]}})
    assert "No tool here is called 'web_search'" in got["error"]


async def test_validation_fails_a_forced_tool_and_warns_on_an_agents():
    from core.builder_guard import missing_in

    agents = {"a1": {"id": "a1", "name": "Old helper", "tools": ["list_agents", "gone_tool"]}}
    workflow = {"steps": [
        {"id": "s1", "name": "Fetch", "type": "tool", "forced_tool": "fetch_invoices", "agent_id": "a1"},
        {"id": "s2", "name": "Help", "type": "agent", "agent_id": "a1"},
    ]}
    issues, warnings = await missing_in([workflow], agents, _Server())
    assert len(issues) == 1 and "fetch_invoices" in issues[0] and "first run would fail" in issues[0]
    assert len(warnings) == 1 and "gone_tool" in warnings[0] and "Old helper" in warnings[0]
