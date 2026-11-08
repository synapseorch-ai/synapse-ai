"""The AI builder's deletes do what the routes' deletes do (F469).

The builder's `delete_orchestration` (and `finish_build`'s clean-up) called the
store's delete, a `DELETE` of the row. Cloud's own route keeps the row and
marks it `is_active = false`, because runs name their workflow — the console
joins on it — and its audit trail records `orchestration.deleted`. A workflow the
builder deleted in cloud lost its runs' name and left no audit row (C7,
code-read, D270). And the builder's `delete_agent` left the agent's memory behind,
which the route wipes (F297).

Naveen's call (D276): a store-level switch a host that keeps rows turns on, and
an observer it is told through after each delete — both off here, so the
self-hosted product, whose screens list every workflow row, deletes as before.
"""
import json
from types import SimpleNamespace

import pytest

from _fakes import seed as S


async def _in_run(tool, args, run_id):
    """A builder tool as the engine calls it inside a run, which it names."""
    from core.builder_tools import execute_builder_tool_with_events
    output, _ = await execute_builder_tool_with_events(tool, args, SimpleNamespace(agent_sessions={}),
                                                       run_id=run_id)
    return json.loads(output)


async def _builder(tool, args):
    from core.builder_tools import execute_builder_tool
    return json.loads(await execute_builder_tool(tool, args, SimpleNamespace(agent_sessions={})))


@pytest.fixture
def host_keeps_rows():
    """A host's install, as cloud's `engine_hooks` does it; put back after."""
    from core.store import resources
    told: list[tuple[str, str, str | None]] = []

    async def observe(kind, id_, doc):
        told.append((kind, id_, (doc or {}).get("name")))

    resources.set_delete_keeps_rows(True)
    resources.set_deleted_observer(observe)
    yield told
    resources.set_delete_keeps_rows(False)
    resources.set_deleted_observer(None)


class TestSelfHostedAsBefore:
    async def test_the_builders_delete_removes_the_row(self):
        from core.store.models import OrchestrationDB  # noqa: F401 — the table the row lived in
        from core.store.resources import get_orchestration
        await S.seed_orchestrations([S.make_orchestration(id="orch_loose", name="Loose")])

        assert (await _builder("delete_orchestration", {"orch_id": "orch_loose", "confirm": True}))["status"]

        assert await get_orchestration("orch_loose") is None


class TestAHostThatKeepsRows:
    async def test_a_deleted_workflow_is_kept_as_a_record_and_told(self, host_keeps_rows):
        from core.store.resources import get_orchestration
        await S.seed_orchestrations([S.make_orchestration(id="orch_k1", name="Month end")])

        result = await _builder("delete_orchestration", {"orch_id": "orch_k1", "confirm": True})

        assert result.get("status") == "deleted", result
        kept = await get_orchestration("orch_k1")
        assert kept is not None and kept["is_active"] is False
        assert host_keeps_rows == [("orchestration", "orch_k1", "Month end")]

    async def test_one_kept_as_a_record_is_not_deleted_or_told_again(self, host_keeps_rows):
        from core.store.resources import delete_orchestration
        await S.seed_orchestrations([S.make_orchestration(id="orch_k2", name="Old", is_active=False)])

        assert await delete_orchestration("orch_k2") is False
        assert host_keeps_rows == []

    async def test_finish_builds_clean_up_keeps_the_row_too(self, host_keeps_rows):
        """The skeleton a build left unvalidated is removed through the same delete (F156)."""
        from core.store.resources import get_orchestration
        made = await _in_run("create_orchestration_skeleton", {"name": "Half", "entry_step_id": "step_a"},
                             "run_build1")
        orch_id = made["orchestration"]["id"]

        done = await _in_run("finish_build", {}, "run_build1")

        assert done["removed"] == [orch_id]
        kept = await get_orchestration(orch_id)
        assert kept is not None and kept["is_active"] is False
        assert host_keeps_rows == [("orchestration", orch_id, "Half")]

    async def test_a_deleted_agent_is_told(self, host_keeps_rows):
        from core.store.resources import get_agent
        await S.seed_agents([S.make_agent(id="agent_k1", name="Front desk")])

        result = await _builder("delete_agent", {"agent_id": "agent_k1", "confirm": True})

        assert result.get("status") == "deleted", result
        assert await get_agent("agent_k1") is None  # agents have no row to keep
        assert host_keeps_rows == [("agent", "agent_k1", "Front desk")]


class TestADeletedToolIsTold:
    """F1127: a tool deleted through the store (a build's made tools, removed on "build without", "stop", a give-up or
    a cancel) told the host nothing, so cloud kept the tool's stored key (`tool:{id}:…` in `org_secrets`), still
    decryptable, with no audit row. Workflows and agents always told it."""

    async def test_by_name_and_by_id(self, host_keeps_rows):
        from core.store.resources import delete_tool, delete_tool_named, get_tool, load_tools, save_tool

        await save_tool({"id": "tool_k1", "name": "acme_send", "url": "https://api.acme.example/send"})
        await save_tool({"id": "tool_k2", "name": "acme_list", "url": "https://api.acme.example/list"})

        assert await delete_tool_named("acme_send") is True
        assert await delete_tool("tool_k2") is True

        assert await get_tool("tool_k1") is None and await load_tools() == []
        assert host_keeps_rows == [("tool", "tool_k1", "acme_send"), ("tool", "tool_k2", "acme_list")]

    async def test_one_that_is_not_there_is_not_told(self, host_keeps_rows):
        from core.store.resources import delete_tool, delete_tool_named

        assert await delete_tool_named("nobody") is False and await delete_tool("tool_none") is False
        assert host_keeps_rows == []

    async def test_with_no_host_it_is_deleted_as_before(self):
        from core.store.resources import delete_tool_named, get_tool, save_tool

        await save_tool({"id": "tool_s1", "name": "acme_send", "url": "https://api.acme.example/send"})
        assert await delete_tool_named("acme_send") is True
        assert await get_tool("tool_s1") is None


class TestTheBuildersAgentDeleteWipesItsMemory:
    async def test_as_the_route_does(self):
        """An agent created later with the same id must not start out knowing this one's memories (F297)."""
        from core import agent_memory
        await S.seed_agents([S.make_agent(id="agent_mem", name="Remembers")])
        await agent_memory.write_memory("agent_mem", "note", "the door code is 4471")

        await _builder("delete_agent", {"agent_id": "agent_mem", "confirm": True})

        assert agent_memory.read_memory("agent_mem", "note") is None
