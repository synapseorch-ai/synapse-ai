"""The AI builder's own builds let nothing that changes something outside run with nobody approving it (decision 4,
BWA1, D274).

BWA1's baselines: a booking API's POST tool and a payment step ran in builder-made workflows with no approval
anywhere. `builder_guard.unapproved`, run by `validate_orchestration`:

- a tool step whose tool acts (declares no read-only) needs a Human step before it on every path, or the step's
  agent asking first;
- an agent the build made, whose tools act, asks first for each (`tool:<name>: confirm` or its category); with every
  tool or a group, it names its tools instead;
- what the approved plan lists under "Acts without asking" may (a nightly post the person said needs no one);
- an agent the build reuses, and every gap in a workflow it only edits, is a warning, never a new refusal.
"""
from __future__ import annotations

import pytest

from core.builder_guard import exempt_in_plan, unapproved
from core.tools import hinted


class _Server:
    agent_sessions: dict = {}


@pytest.fixture
async def tools():
    from core.routes.tools import _upsert_custom_tools

    await _upsert_custom_tools([
        hinted({"name": "create_booking", "description": "Book.", "method": "POST", "url": "https://b.example/x"}),
        hinted({"name": "search_rooms", "description": "Search.", "method": "GET", "url": "https://b.example/s"}),
    ])


def _flow(*steps, entry="s1"):
    return {"id": "orch_x", "entry_step_id": entry, "steps": list(steps)}


TOOL_STEP = {"id": "s2", "name": "Book it", "type": "tool", "forced_tool": "create_booking", "next_step_id": "end"}
END = {"id": "end", "type": "end"}


async def test_a_tool_step_that_acts_needs_a_person_before_it(tools):
    bare = _flow({"id": "s1", "type": "llm", "next_step_id": "s2"}, TOOL_STEP, END)
    issues, _ = await unapproved([bare], {}, _Server(), made_agents=set(), exempt=set())
    assert len(issues) == 1 and "`create_booking`" in issues[0] and "nobody approving it" in issues[0]

    asked = _flow({"id": "s1", "type": "human", "next_step_id": "s2"}, TOOL_STEP, END)
    assert (await unapproved([asked], {}, _Server(), made_agents=set(), exempt=set()))[0] == []


async def test_a_read_only_tool_needs_nobody(tools):
    reads = _flow({**TOOL_STEP, "id": "s1", "forced_tool": "search_rooms"}, END)
    assert (await unapproved([reads], {}, _Server(), made_agents=set(), exempt=set()))[0] == []


async def test_an_agent_the_build_made_asks_first_for_what_acts(tools):
    step = {"id": "s1", "name": "Book", "type": "agent", "agent_id": "a1", "next_step_id": "end"}
    loose = {"id": "a1", "name": "Booker", "tools": ["search_rooms", "create_booking"]}
    issues, _ = await unapproved([_flow(step, END)], {"a1": loose}, _Server(), made_agents={"a1"}, exempt=set())
    assert len(issues) == 1 and '{"tool:create_booking": "confirm"}' in issues[0]

    careful = {**loose, "tool_policy": {"tool:create_booking": "confirm"}}
    assert (await unapproved([_flow(step, END)], {"a1": careful}, _Server(), made_agents={"a1"}, exempt=set()))[0] == []


async def test_every_tool_must_be_named_by_an_agent_the_build_made(tools):
    step = {"id": "s1", "name": "Do", "type": "agent", "agent_id": "a1", "next_step_id": "end"}
    issues, _ = await unapproved([_flow(step, END)], {"a1": {"id": "a1", "name": "All", "tools": ["all"]}}, _Server(),
                                 made_agents={"a1"}, exempt=set())
    assert issues and "by name" in issues[0]


async def test_what_the_plan_lets_act_without_asking_may(tools):
    plan = "## Steps\n1. Book\n\n## Acts without asking\n- `create_booking` — you said not to ask each night.\n"
    bare = _flow({"id": "s1", "type": "llm", "next_step_id": "s2"}, TOOL_STEP, END)
    assert exempt_in_plan(plan) == {"create_booking"}
    assert (await unapproved([bare], {}, _Server(), made_agents=set(), exempt=exempt_in_plan(plan)))[0] == []


async def test_a_reused_agent_and_an_edit_are_warned_never_refused(tools):
    step = {"id": "s1", "name": "Book", "type": "agent", "agent_id": "a9", "next_step_id": "end"}
    theirs = {"id": "a9", "name": "Front desk", "tools": ["create_booking"]}
    issues, warnings = await unapproved([_flow(step, END)], {"a9": theirs}, _Server(), made_agents=set(), exempt=set())
    assert issues == [] and "Front desk" in warnings[0]

    bare = _flow({"id": "s1", "type": "llm", "next_step_id": "s2"}, TOOL_STEP, END)
    issues, warnings = await unapproved([bare], {}, _Server(), made_agents=set(), exempt=set(), strict=False)
    assert issues == [] and "`create_booking`" in warnings[0]


async def test_the_builder_can_give_an_agent_its_ask_first():
    import json

    from core.builder_tools import execute_builder_tool

    made = json.loads(await execute_builder_tool("create_agent", {
        "name": "Booker", "description": "d", "type": "conversational", "tools": ["list_agents"],
        "system_prompt": "s", "tool_policy": {"tool:list_agents": "confirm", "junk": "x"}}, _Server()))
    assert made["agent"]["tool_policy"] == {"tool:list_agents": "confirm"}
