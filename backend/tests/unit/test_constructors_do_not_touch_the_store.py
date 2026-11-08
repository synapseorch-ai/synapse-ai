"""
Two constructors used to read resources, and `__init__` cannot await.

`OrchestrationEngine.__init__` called `load_user_agents()` and
`MCPClientManager.__init__` read `mcp_servers.json`. Once those collections
moved into an async store, the obvious fixes were both bad: making the
constructors async would touch every construction site — nine in the engine's
case plus thirteen in tests — and a lazily-awaited attribute would change the
type of something read as a plain dict in eight places.

Instead both constructors build empty and the async entry point fills them in.
These tests pin that: constructing must not need a store, and the value must be
populated by the time it is used.
"""
from contextlib import AsyncExitStack

import pytest

from core.mcp_client import MCPClientManager
from core.models_orchestration import Orchestration
from core.orchestration.engine import OrchestrationEngine
from core.store import resources


def _orch() -> Orchestration:
    return Orchestration.model_validate({
        "id": "orch-1",
        "name": "Test",
        "entry_step_id": "s1",
        "steps": [{
            "id": "s1",
            "name": "Say",
            "type": "print",
            "print_content": "hello",
            "output_key": "out",
            "next_step_id": None,
        }],
    })


# ── OrchestrationEngine ──────────────────────────────────────────────────────

def test_engine_constructs_without_reading_the_store():
    """No await, no event loop, no database — this runs in a sync test."""
    engine = OrchestrationEngine(_orch(), server_module=None)

    assert engine.agent_names == {}


@pytest.mark.asyncio
async def test_agent_names_are_populated_by_the_time_the_run_emits():
    await resources.save_agent({"id": "a1", "name": "Researcher"})
    engine = OrchestrationEngine(_orch(), server_module=None)

    events = []
    async for event in engine.run("go", run_id="run-agent-names"):
        events.append(event)
        if len(events) >= 1:
            break

    assert engine.agent_names == {"a1": "Researcher"}


@pytest.mark.asyncio
async def test_agent_names_survives_a_store_failure(monkeypatch):
    """Attribution is cosmetic; a store hiccup must not kill the run."""
    async def _boom():
        raise RuntimeError("store down")

    monkeypatch.setattr("core.routes.agents.load_user_agents", _boom)
    engine = OrchestrationEngine(_orch(), server_module=None)

    assert await engine._load_agent_names() == {}


# ── MCPClientManager ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_manager_constructs_empty_and_loads_on_demand():
    await resources.save_mcp_server({"name": "github", "label": "GitHub"})

    async with AsyncExitStack() as stack:
        manager = MCPClientManager(stack)
        assert manager.servers_config == []

        await manager.load()
        assert [s["name"] for s in manager.servers_config] == ["github"]


@pytest.mark.asyncio
async def test_status_writes_reach_the_store():
    await resources.save_mcp_server({"name": "github", "label": "GitHub"})

    async with AsyncExitStack() as stack:
        manager = MCPClientManager(stack)
        await manager.load()
        await manager._set_status("github", "connected")

    assert (await resources.get_mcp_server("github"))["status"] == "connected"
