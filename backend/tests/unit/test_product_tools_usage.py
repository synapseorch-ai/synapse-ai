"""`get_usage`: what the workspace spent on models, for any member, through the registry (CLI1, D294).

Naveen (2026-10-17): usage reaches a key as a product tool, not a route, so MCP clients and the CLI read the same
figures the Usage screen shows: estimated dollars, tokens and calls over a window, by day, agent or model.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.product_tools import areas, registry, usage
from core.product_tools.caller import Caller
from core.product_tools.registry import ToolRefusal, invoke
from core.product_tools.roles import Role


async def _log(model="gpt-4.1-mini", agent_id="agent_a", tin=1000, tout=100, **kw):
    from core.usage_tracker import log_usage
    await log_usage(model=model, provider="openai", input_tokens=tin, output_tokens=tout, context_chars=0,
                    session_id="s", agent_id=agent_id, source="chat", **kw)


def _viewer() -> Caller:
    return Caller(role=Role.VIEWER, via="mcp")


@pytest.fixture(autouse=True)
def _loaded():
    areas.ensure_loaded()
    usage.set_usage_source(None)
    yield
    usage.set_usage_source(None)


class TestGetUsage:
    def test_it_is_a_read_any_member_may_make(self):
        tool = registry.get("get_usage")
        assert tool is not None and tool.kind == "read" and tool.scope == "read"
        assert tool.min_role == Role.VIEWER and not tool.app_only and tool.area == "usage"

    async def test_by_model_it_sums_each_models_calls(self):
        await _log(tin=1000, tout=100)
        await _log(tin=500, tout=50)
        await _log(model="claude-haiku-4-5", tin=10, tout=1)
        got = await invoke("get_usage", _viewer(), {"by": "model", "days": 7})
        rows = {r["key"]: r for r in got["rows"]}
        assert (rows["gpt-4.1-mini"]["input_tokens"], rows["gpt-4.1-mini"]["output_tokens"],
                rows["gpt-4.1-mini"]["calls"]) == (1500, 150, 2)
        assert got["total"]["calls"] == 3 and got["total"]["input_tokens"] == 1510

    async def test_by_agent_it_names_each_agent(self, seed_agent):
        agent = await seed_agent(name="Spender")
        await _log(agent_id=agent["id"])
        got = await invoke("get_usage", _viewer(), {"by": "agent"})
        row = next(r for r in got["rows"] if r.get("agent_id") == agent["id"])
        assert row["key"] == "Spender"

    async def test_by_day_oldest_first_and_a_compaction_costs_nothing(self):
        from core.usage_tracker import log_compaction_event
        await _log()
        await log_compaction_event(stage="trim", chars_before=10, chars_after=5, session_id="s",
                                   agent_id="agent_a", model="gpt-4.1-mini")
        got = await invoke("get_usage", _viewer(), {"by": "day"})
        assert sum(r["calls"] for r in got["rows"]) == 1

    async def test_the_window_is_kept_to(self):
        from sqlalchemy import update

        from core.store import session
        from core.store.models import UsageLogDB

        await _log(tin=7)
        async with session() as s:
            await s.execute(update(UsageLogDB).values(timestamp=datetime.now(timezone.utc) - timedelta(days=30)))
            await s.commit()
        await _log(tin=3)
        got = await invoke("get_usage", _viewer(), {"days": 7})
        assert got["total"]["input_tokens"] == 3

    async def test_a_grouping_it_does_not_know_is_refused(self):
        with pytest.raises(ToolRefusal, match="`by`"):
            await invoke("get_usage", _viewer(), {"by": "week"})

    async def test_a_host_brings_its_own_rows(self):
        seen = []

        async def cloud(since, by):
            seen.append(by)
            return [{"key": "m", "cost_usd": 1.5, "input_tokens": 1, "output_tokens": 2, "calls": 3}]
        usage.set_usage_source(cloud)
        got = await invoke("get_usage", _viewer(), {"by": "model"})
        assert seen == ["model"] and got["total"]["cost_usd"] == 1.5
