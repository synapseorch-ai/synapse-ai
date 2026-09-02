"""
A step that names an agent the tenant does not have must fail, not run another one.

`_resolve_agent_by_id` is the last stop for every agent lookup in the engine —
both the orchestration step path and the chat path go through it when the
resource provider answers with nothing. Until this was pinned, that "nothing"
fell through to `get_active_agent_data()`, which returns the tenant's *first*
agent: a workflow step naming a deleted agent ran whichever agent sorted first,
with its prompt and tools, and no event said so. `core/scale/context.py`
promises the opposite ("an empty answer is an answer"); these tests hold the
engine to it.
"""
import pytest

from core import react_engine
from core.scale.context import set_resource_provider


class _KnowsNoAgents:
    """A provider whose tenant has no agent by any id."""

    async def resolve_agent(self, agent_id):
        return None

    async def resolve_orchestration(self, orch_id):
        return None

    async def resolve_custom_tools(self):
        return []

    async def resolve_mcp_servers(self):
        return []


@pytest.fixture
def provider():
    set_resource_provider(_KnowsNoAgents())
    yield
    set_resource_provider(None)


@pytest.fixture
def active_agent(monkeypatch):
    """Stand in for the single-tenant fallback, and record whether it was asked."""
    calls: list[int] = []

    async def _active():
        calls.append(1)
        return {"id": "agent_first", "name": "First"}

    monkeypatch.setattr("core.routes.agents.get_active_agent_data", _active)
    return calls


@pytest.mark.asyncio
async def test_a_missing_agent_is_an_error_under_a_provider(provider, active_agent):
    """The miss is the answer: no silent substitution, and the id is in the message."""
    with pytest.raises(RuntimeError, match="agent_ghost"):
        await react_engine._resolve_agent_by_id("agent_ghost")
    assert active_agent == [], "the active-agent fallback must not be consulted"


@pytest.mark.asyncio
async def test_no_agent_id_still_means_the_active_agent_under_a_provider(provider, active_agent):
    """A chat with no agent selected is a different question, and keeps its answer."""
    assert (await react_engine._resolve_agent_by_id(None))["id"] == "agent_first"
    assert active_agent == [1]


@pytest.mark.asyncio
async def test_without_a_provider_a_stale_id_falls_back(active_agent):
    """Single-tenant installs keep the old behaviour — there is no other tenant to leak."""
    assert (await react_engine._resolve_agent_by_id("agent_ghost"))["id"] == "agent_first"
    assert active_agent == [1]
