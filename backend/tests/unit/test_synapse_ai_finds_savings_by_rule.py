"""Synapse AI finds savings by code's rules, each with its estimate and the change that makes it (F1261, REACH1).

Naveen, 10-30: "usage to give suggestions", each a ready change card. These pin that no saving is a model's guess:

- a model swap is offered only where F1251's best fit for what the agent needs costs less, priced from that agent's
  own tokens over the window, and its op sets that model;
- one that saves less than the floor a month, or whose fit costs more, is not offered;
- a schedule is "repeating itself" only when at least 80% of its last runs answered exactly as the run before or
  failed, said with the count; its new time is the person's to say;
- prompt caching off is said with no number, and why there is none;
- topic `savings` answers them, largest first.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from core import savings
from core.builder_tools import execute_builder_tool_with_events
from core.native_builder import models_menu as MM


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


def _entry(id, price_in, price_out, *, reasoning=False, source="key"):
    return MM.ModelEntry(id=id, name=id, provider="p", source=source, tools="native", vision=False,
                         reasoning=reasoning, price_in=price_in, price_out=price_out, can_pay=True)


MENU = [_entry("big-model", 15.0, 75.0), _entry("small-model", 0.8, 4.0)]


@pytest.fixture(autouse=True)
def settings_from_the_store():
    from core import settings_runtime
    from core.config import get_settings_provider, set_settings_provider
    from core.product_tools import usage

    previous = get_settings_provider()
    settings_runtime.reset_state()
    settings_runtime.install_provider()
    yield
    usage.set_usage_source(None)
    settings_runtime.reset_state()
    set_settings_provider(previous)


@pytest.fixture
async def an_agent_on_a_big_model(monkeypatch):
    from core.product_tools import usage
    from core.store.resources import save_agent
    from tests._fakes.seed import make_agent

    agent = make_agent(name="Summariser", model="big-model", tools=["web_search"])
    await save_agent(agent)

    async def menu():
        return MENU

    async def source(since, by):
        assert by == "agent"
        # 30 days of 2M in, 0.2M out: $45 at big-model's rates, $2.40 at small-model's.
        return [{"key": agent["id"], "cost_usd": 45.0, "input_tokens": 2_000_000, "output_tokens": 200_000,
                 "calls": 400}]

    monkeypatch.setattr(MM, "menu", menu)
    monkeypatch.setattr(MM, "workspace_model", lambda: "small-model")
    usage.set_usage_source(source)
    return agent


class TestAModelSwap:
    async def test_the_best_fit_costing_less_is_offered_with_its_estimate_and_op(self, client, an_agent_on_a_big_model):
        [swap] = await savings.model_swaps(30)
        assert (swap["agent"], swap["now"], swap["to"]) == ("Summariser", "big-model", "small-model")
        assert swap["saves_usd_month"] == pytest.approx(42.6)
        assert swap["op"] == {"op": "set_agent_model", "target": {"kind": "agent", "id": an_agent_on_a_big_model["id"]},
                              "fields": {"model": "small-model"}}
        assert "best fit" in swap["rule"]

    async def test_a_saving_under_the_floor_is_not_offered(self, client, an_agent_on_a_big_model, monkeypatch):
        from core.product_tools import usage

        async def tiny(since, by):
            return [{"key": an_agent_on_a_big_model["id"], "cost_usd": 0.2, "input_tokens": 10_000,
                     "output_tokens": 1_000, "calls": 3}]

        usage.set_usage_source(tiny)
        assert await savings.model_swaps(30) == []

    async def test_a_fit_that_costs_more_is_not_offered(self, client, an_agent_on_a_big_model, monkeypatch):
        async def menu():
            return [_entry("big-model", 0.5, 2.0), _entry("small-model", 0.8, 4.0)]

        monkeypatch.setattr(MM, "menu", menu)
        assert await savings.model_swaps(30) == []


class TestARepeatingSchedule:
    async def _seed(self, monkeypatch, answers):
        from core.orchestration import run_view
        from core.store import schedules

        await schedules.save({"id": "sched_d", "name": "Hourly digest", "enabled": True,
                              "target_type": "orchestration", "target_id": "orch_d", "prompt": "Go",
                              "schedule_type": "interval", "interval_value": 1, "interval_unit": "hours"})
        now = datetime.now(timezone.utc)
        rows = [{"run_id": f"r{i}", "session_id": "schedule_sched_d", "status": "completed",
                 "started_at": (now - timedelta(hours=i)).isoformat(), "total_cost_usd": 0.05}
                for i in range(len(answers))]

        async def list_runs(limit=20, **kw):
            return rows

        async def read_run(run_id):
            return {"status": "completed", "response": answers[int(run_id[1:])]}

        monkeypatch.setattr(run_view, "list_runs", list_runs)
        monkeypatch.setattr(run_view, "read_run", read_run)

    async def test_one_answering_the_same_is_offered_with_the_count(self, client, monkeypatch):
        await self._seed(monkeypatch, ["Nothing new."] * 9 + ["Two new orders."])
        [found] = await savings.repeating_schedules(30)
        assert found["schedule"] == "Hourly digest"
        assert found["rule"].startswith("8 of its last 10 runs answered exactly as the run before or failed")
        assert found["op"]["op"] == "set_schedule" and found["op"]["ask"] == "how often it should run instead"

    async def test_one_whose_answers_change_is_not(self, client, monkeypatch):
        await self._seed(monkeypatch, [f"Update {i}" for i in range(10)])
        assert await savings.repeating_schedules(30) == []

    async def test_too_few_runs_say_nothing(self, client, monkeypatch):
        await self._seed(monkeypatch, ["Same."] * 3)
        assert await savings.repeating_schedules(30) == []


class TestCaching:
    async def test_off_is_said_with_no_number_and_why(self, client):
        from core.models import Settings
        from core.routes.settings import update_settings

        await update_settings(Settings(prompt_cache_enabled=False))
        [found] = await savings.caching()
        assert found["saves_usd_month"] is None and "does not hold" in found["no_estimate"]
        assert found["op"]["fields"] == {"prompt_cache_enabled": True}

    async def test_on_says_nothing(self, client):
        assert await savings.caching() == []


async def test_the_topic_answers_largest_first(client, an_agent_on_a_big_model):
    from core.models import Settings
    from core.routes.settings import update_settings

    await update_settings(Settings(prompt_cache_enabled=False))
    raw, _ = await execute_builder_tool_with_events("read_workspace", {"topic": "savings"}, _Server())
    got = json.loads(raw)
    assert [i["kind"] for i in got["items"]] == ["model", "caching"]
    assert got["window"] == "the last 30 days"
