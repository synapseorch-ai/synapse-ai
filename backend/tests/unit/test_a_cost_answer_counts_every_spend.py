"""A cost answer counts every spend the Usage screen counts (F1297, REACH1; CAP3's prod-08 on f862b5b6, 79 11-02).

Asked "what is costing me the most", Synapse AI read topic usage and said "Brightloom Feature Post Generator: 1 run,
$0.031 … That's the total — roughly 3 cents", while 40 of the Greeter agent's chat calls on gpt-4.1 (≈ $0.36) sat in
the usage log: the topic summed workflow runs only. The product tool get_usage (MCP, the CLI) left out embeddings,
which the screen counts. These pin the class, one source for every cost answer:

- get_usage counts what the Usage screen counts: every call but a compaction event, embeddings in;
- topic usage's total is get_usage's, which is the screen's for the same window: runs, chats and embeddings, never
  a sum of runs alone; the agents and the models it went on are named, Greeter's chats among them;
- the savings topic names an agent that costs more than the floor when no cheaper model with a listed price fits
  it, with no number and no op, rather than saying nothing; and reads its usage through get_usage, as the person
  (a host's source answers only for a bound caller).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from core.builder_tools import execute_builder_tool_with_events
from core.native_builder import models_menu as MM


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


async def _row(**values):
    from core.store import usage as store

    stamp = (datetime.now(timezone.utc) - timedelta(hours=values.pop("hours_ago", 1))).isoformat()
    await store.append(store.record_values({"timestamp": stamp, "provider": "openai", "input_tokens": 1000,
                                            "output_tokens": 100, "context_chars": 0, **values}))


@pytest.fixture
async def a_mix():
    """prod-08's mix: one workflow run's calls, 40 chat calls by an agent, one embedding, one compaction."""
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.state import SharedState
    from core.store.resources import save_agent, save_orchestration
    from tests._fakes.seed import make_agent, make_orchestration

    greeter = make_agent(id="agent_greeter", name="Greeter", model="gpt-4.1")
    await save_agent(greeter)
    await save_orchestration(make_orchestration(id="orch_post", name="Feature Post Generator"))
    started = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await SharedState(OrchestrationRun(run_id="run_post", orchestration_id="orch_post", status="completed",
                                       started_at=started, ended_at=started, total_cost_usd=0.031)).checkpoint()
    await _row(model="gpt-4.1-mini", estimated_cost=0.031, run_id="run_post", source="orchestration",
               agent_id="agent_writer", session_id="s_run")
    for _ in range(40):
        await _row(model="gpt-4.1", estimated_cost=0.009, agent_id="agent_greeter", session_id="s_chat",
                   source="chat")
    await _row(model="text-embedding-3-small", estimated_cost=0.002, event_type="embedding", source="cache",
               input_tokens=500, output_tokens=0)
    await _row(model="gpt-4.1", estimated_cost=0.0, event_type="compaction", source="chat", agent_id="agent_greeter")
    return {"run": 0.031, "chats": round(40 * 0.009, 6), "embedding": 0.002}


async def _usage(by: str = "agent", days: int = 30) -> dict:
    from core.product_tools import areas, registry
    from core.product_tools.caller import Caller
    from core.product_tools.roles import Role

    areas.ensure_loaded()
    return await registry.invoke("get_usage", Caller(role=Role.VIEWER, via="assistant"), {"by": by, "days": days})


class TestGetUsage:
    async def test_it_counts_what_the_screen_counts(self, client, a_mix):
        from core.usage_tracker import get_usage_summary

        screen = await get_usage_summary()
        got = await _usage("model")
        assert got["total"]["cost_usd"] == pytest.approx(screen["total_cost"])
        assert got["total"]["cost_usd"] == pytest.approx(sum(a_mix.values()))
        assert "text-embedding-3-small" in [r["key"] for r in got["rows"]], "the embedding is spend too"


class TestTheUsageTopic:
    async def _read(self, **args) -> dict:
        raw, _ = await execute_builder_tool_with_events("read_workspace", {"topic": "usage", "days": 30, **args},
                                                        _Server())
        return json.loads(raw)

    async def test_its_total_is_the_screens_never_the_runs_alone(self, client, a_mix):
        from core.usage_tracker import get_usage_summary

        got = await self._read()
        assert got["total"]["cost_usd"] == pytest.approx((await get_usage_summary())["total_cost"])
        assert got["total"]["cost_usd"] > a_mix["run"] * 10, "40 chat calls are in it, not one run's 3 cents"
        greeter = next(a for a in got["by_agent"] if a["agent"] == "Greeter")
        assert greeter["cost_usd"] == pytest.approx(a_mix["chats"]) and greeter["calls"] == 40
        assert got["by_agent"][0]["agent"] == "Greeter", "the costliest first"
        assert any(m["model"] == "gpt-4.1" for m in got["by_model"])
        # The workflows' runs are still said, beside the whole.
        assert [w["workflow"] for w in got["items"]] == ["Feature Post Generator"]
        assert "chats" in got["note"] and "runs" in got["note"]


class TestSavingsNamesWhatItCantPrice:
    @pytest.fixture
    def an_unpriced_menu(self, monkeypatch):
        def entry(id, price_in, price_out):
            return MM.ModelEntry(id=id, name=id, provider="p", source="key", tools="native", vision=False,
                                 reasoning=False, price_in=price_in, price_out=price_out, can_pay=True)

        async def menu():
            return [entry("gpt-4.1", 2.0, 8.0), entry("local-small", None, None)]

        monkeypatch.setattr(MM, "menu", menu)
        monkeypatch.setattr(MM, "workspace_model", lambda: "local-small")

    async def test_a_costly_agent_with_no_cheaper_priced_fit_is_named_without_a_number(
        self, client, a_mix, an_unpriced_menu
    ):
        from core import savings

        # $0.36 in 7 days is about $1.54 a month, over the floor.
        found = [s for s in await savings.model_swaps(7) if s.get("agent") == "Greeter"]
        assert found, "a costly agent is never left out because its best fit has no price"
        [said] = found
        assert said["saves_usd_month"] is None and said["op"] is None
        assert "no cheaper model with a listed price" in said["rule"] and "gpt-4.1" in said["rule"]
