"""A builder run does only what the person it works for may do (ASSIST1, D291, F681).

Every role can talk to the assistant now, and a builder run asks its questions through the run's human-input door,
which an approver may answer. So nothing in the run's prompts or plan review can be what stops an approver or a
viewer from having it write: the operator would apply an approved plan for anyone who could approve it. The rule is
in code, at the one place every builder tool call passes (`execute_builder_tool_with_events`):

- every builder tool is a read, a card (it shows the person something to click; it changes nothing) or a write, and
  a tool that is none of the three fails this test;
- a write is refused when the run's person, by their role now (not when the run started), is below builder;
- a run with no person on it (anything but the assistant's) is not touched.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import BUILDER_TOOL_NAMES, execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState
from core.product_tools import build, hooks
from core.product_tools.roles import Role


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def roles():
    """A host with roles: each person's role now, changeable mid-test."""
    held: dict[str, Role] = {}

    async def source(user_id: str):
        return held.get(user_id)

    hooks.set_role_source(source)
    builder_access.forget_runs()
    yield held
    hooks.set_role_source(None)
    builder_access.forget_runs()


async def _run_for(user_id: str | None, run_id: str = "builder_abc") -> str:
    state = {builder_access.CALLER_KEY: {"user_id": user_id}} if user_id is not ... else {}
    await SharedState(OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder",
                                       shared_state=state)).checkpoint()
    return run_id


async def _call(tool: str, args: dict, run_id: str | None) -> dict:
    out, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run_id)
    return json.loads(out)


def test_every_builder_tool_is_a_read_a_card_or_a_write():
    kinds = builder_access.KINDS
    assert not (builder_access.READS & builder_access.CARDS)
    assert not (builder_access.READS & builder_access.WRITES)
    assert not (builder_access.CARDS & builder_access.WRITES)
    assert set(BUILDER_TOOL_NAMES) - set(kinds) == set(), "a builder tool with no kind: say what it is"
    # The MCP server's own lists (MCP2) say the same of the tools they serve.
    assert set(build.READS) <= builder_access.READS
    assert set(build.ACTS) <= builder_access.WRITES


@pytest.mark.parametrize("role", [Role.VIEWER, Role.APPROVER])
async def test_a_person_below_builder_cannot_have_the_run_write(roles, role):
    roles["u1"] = role
    run_id = await _run_for("u1")

    refused = await _call("create_agent", {"name": "Sneaky", "system_prompt": "x"}, run_id)
    assert "error" in refused and "builder" in refused["error"] and role.label in refused["error"]

    from core.store.resources import load_agents
    assert not [a for a in await load_agents() if a.get("name") == "Sneaky"]

    # Reading is still theirs.
    assert "error" not in await _call("list_agents", {}, run_id)


async def test_a_builder_may(roles):
    roles["u1"] = Role.BUILDER
    run_id = await _run_for("u1")
    made = await _call("create_agent", {"name": "Fine", "system_prompt": "x"}, run_id)
    assert "error" not in made, made


async def test_the_role_is_read_on_every_call_not_when_the_run_started(roles):
    roles["u1"] = Role.ADMIN
    run_id = await _run_for("u1")
    assert "error" not in await _call("create_agent", {"name": "First", "system_prompt": "x"}, run_id)

    roles["u1"] = Role.VIEWER  # demoted while the run waited for its review
    assert "error" in await _call("create_agent", {"name": "Second", "system_prompt": "x"}, run_id)

    roles.pop("u1")  # removed from the workspace
    gone = await _call("create_agent", {"name": "Third", "system_prompt": "x"}, run_id)
    assert "error" in gone and "member" in gone["error"]


async def test_a_run_without_a_person_is_untouched(roles):
    run_id = await _run_for(..., run_id="run_plain")  # a workflow's run: no `_caller`
    assert "error" not in await _call("create_agent", {"name": "Plain", "system_prompt": "x"}, run_id)
    assert "error" not in await _call("create_agent", {"name": "NoRun", "system_prompt": "x"}, None)


async def test_a_tool_sees_who_the_run_works_for(roles):
    roles["u7"] = Role.APPROVER
    run_id = await _run_for("u7")
    seen = {}

    async def peek(args, server_module, events):
        caller = builder_access.current_caller()
        seen.update(user=caller.user_id, role=caller.role, via=caller.via)
        return {"ok": True}

    from core import builder_tools
    builder_tools._REGISTERED["read_workspace"], keep = peek, builder_tools._REGISTERED["read_workspace"]
    try:
        await _call("read_workspace", {"topic": "agents"}, run_id)
    finally:
        builder_tools._REGISTERED["read_workspace"] = keep
    assert seen == {"user": "u7", "role": Role.APPROVER, "via": "assistant"}
    assert builder_access.current_caller() is None
