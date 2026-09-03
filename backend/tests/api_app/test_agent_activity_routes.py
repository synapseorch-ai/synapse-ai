"""
The two per-agent reads behind the agent editor's Runs and Metrics tabs (D72).

Neither is new data. Usage rows have carried `agent_id` since the usage log
became a table, and agent run logs record it in their header — they simply were
not addressable per agent. What matters here is that the filters are honest:
`agent_id` on the log list must be applied *before* paging, or a quiet agent
shows empty pages in a busy workspace, and the usage aggregate must count only
its own agent's calls.
"""
import pytest


def _log(agent_id, agent_name, source="chat"):
    """One real agent run log on disk. Returns its run_id."""
    from core.agent_logger import AgentLogger

    logger = AgentLogger(
        agent_id=agent_id,
        agent_name=agent_name,
        session_id=f"sess_{agent_id}",
        source=source,
        user_message=f"input for {agent_name}",
    )
    logger.run_end("completed")
    logger.close()          # flush the writer thread before anything reads it
    return logger.run_id


async def _usage(agent_id, model="gpt-x", cost=0.5, tokens_in=100, tokens_out=20):
    """One usage row, written straight to the store.

    `log_usage` prices the call from the rate card, and what is under test here
    is the aggregate over the rows — not how a row got its cost.
    """
    from datetime import datetime, timezone

    from core.store import usage as usage_store

    await usage_store.append({
        "timestamp": datetime.now(timezone.utc),
        "model": model,
        "provider": "test",
        "session_id": f"sess_{agent_id}",
        "agent_id": agent_id,
        "source": "chat",
        "run_id": None,
        "tool_name": None,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "total_tokens": tokens_in + tokens_out,
        "context_chars": 0,
        "estimated_cost": cost,
        "latency_seconds": 0.0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "estimated_savings": 0.0,
        "response_cache_hit": False,
    })


class TestAgentRunLogs:
    async def test_the_filter_selects_one_agents_runs(self, client):
        mine = _log("agent_a", "Alpha")
        theirs = _log("agent_b", "Beta")

        resp = await client.get("/api/logs/agents?agent_id=agent_a")
        assert resp.status_code == 200
        run_ids = [r["run_id"] for r in resp.json()]
        assert mine in run_ids
        assert theirs not in run_ids

    async def test_the_filter_runs_before_paging(self, client):
        """Filtering after the slice would page over everyone's runs."""
        quiet = _log("agent_quiet", "Quiet")
        for _ in range(5):
            _log("agent_noise", "Noise")

        # A page smaller than the noise, and the quiet run is the oldest of the
        # lot: filtering after the slice would never reach it.
        # Guard the premise: unfiltered, that run is well past the first page.
        first_page = (await client.get("/api/logs/agents?limit=2")).json()
        assert quiet not in [r["run_id"] for r in first_page]

        resp = await client.get("/api/logs/agents?agent_id=agent_quiet&limit=2")
        assert [r["run_id"] for r in resp.json()] == [quiet]

    async def test_no_filter_still_lists_everything(self, client):
        a = _log("agent_a", "Alpha")
        b = _log("agent_b", "Beta")
        listed = (await client.get("/api/logs/agents")).json()
        assert {r["run_id"] for r in listed} >= {a, b}


class TestAgentUsageSummary:
    async def test_totals_count_only_this_agent(self, client):
        await _usage("agent_a", cost=0.25)
        await _usage("agent_a", cost=0.75)
        await _usage("agent_b", cost=9.0)

        body = (await client.get("/api/usage/agents/agent_a")).json()
        assert body["agent_id"] == "agent_a"
        assert body["calls"] == 2
        assert body["estimated_cost"] == pytest.approx(1.0)
        assert body["input_tokens"] == 200
        assert body["output_tokens"] == 40
        assert body["last_used"] is not None

    async def test_the_per_model_breakdown_groups(self, client):
        await _usage("agent_a", model="cheap", cost=0.1)
        await _usage("agent_a", model="cheap", cost=0.1)
        await _usage("agent_a", model="dear", cost=5.0)

        body = (await client.get("/api/usage/agents/agent_a")).json()
        by_model = {m["model"]: m for m in body["by_model"]}
        assert by_model["cheap"]["calls"] == 2
        assert by_model["dear"]["estimated_cost"] == pytest.approx(5.0)
        # Ordered by spend, so the expensive model is the first thing read.
        assert body["by_model"][0]["model"] == "dear"

    async def test_an_agent_with_no_calls_is_zeroed_not_missing(self, client):
        body = (await client.get("/api/usage/agents/agent_never_ran")).json()
        assert body["calls"] == 0
        assert body["estimated_cost"] == 0.0
        assert body["by_model"] == []
        assert body["last_used"] is None

    async def test_the_usage_log_filters_by_agent_too(self, client):
        await _usage("agent_a")
        await _usage("agent_b")
        body = (await client.get("/api/usage/logs?agent_id=agent_a")).json()
        assert body["count"] == 1
        assert body["logs"][0]["agent_id"] == "agent_a"

    async def test_compaction_events_are_not_model_calls(self, client):
        """They share the table; the usage screen's own totals exclude them too."""
        from core.usage_tracker import log_compaction_event

        await _usage("agent_a", cost=1.0)
        await log_compaction_event(
            stage="llm_summary",
            chars_before=1000,
            chars_after=400,
            session_id="sess_agent_a",
            agent_id="agent_a",
        )

        body = (await client.get("/api/usage/agents/agent_a")).json()
        assert body["calls"] == 1
        assert body["estimated_cost"] == pytest.approx(1.0)
        assert [m["model"] for m in body["by_model"]] == ["gpt-x"]
