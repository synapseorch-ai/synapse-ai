"""CE1 (D261): the estimate route and each step's spend on the run detail, in OSS.

`POST /api/orchestrations/estimate` prices the model half of a definition from the
rate card: the workspace's own keys, a local model at nothing, and a model the rate
card does not know named rather than counted free. With finished runs since the
workflow's last edit it answers from them. `GET /api/orchestrations/runs/{id}`
carries `step_usage` (each step, passes summed) and `entry_usage` (each history
entry), swept from the run's calls by their exact windows.
"""
from datetime import UTC, datetime, timedelta

import pytest

from core.orchestration.estimate import TOKEN_PRIOR

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def rate_card():
    """The rate card as an install ships it, for the one model these tests price."""
    from core import usage_tracker

    await usage_tracker.save_pricing_table({"gpt-4o": {"provider": "openai", "input_per_1m": 2.5, "output_per_1m": 10}})


def _definition(model: str) -> dict:
    """Weekly report's shape: write, ask, then one of two arms."""
    return {
        "name": "Weekly",
        "entry_step_id": "write",
        "steps": [
            {"id": "write", "name": "Write", "type": "llm", "model": model, "next_step_id": "review"},
            {"id": "review", "name": "Review", "type": "human", "next_step_id": "gate"},
            {"id": "gate", "name": "Gate", "type": "if_else", "if_true_step_id": "done",
             "if_false_step_id": "revise"},
            {"id": "revise", "name": "Revise", "type": "llm", "model": model, "next_step_id": "done"},
            {"id": "done", "name": "End", "type": "end"},
        ],
    }


def _llm(rate_in: float, rate_out: float, i: int) -> float:
    t = TOKEN_PRIOR["llm"]
    return (t.input[i] * rate_in + t.output[i] * rate_out) / 1_000_000


class TestTheEstimateRoute:
    async def test_own_keys_from_the_rate_card_cheapest_to_dearest_path(self, client):
        resp = await client.post("/api/orchestrations/estimate", json={"definition": _definition("gpt-4o")})
        assert resp.status_code == 200
        body = resp.json()
        side = body["models"]["sides"]["own_keys"]
        # gpt-4o: $2.50 in, $10 out a million. Approved runs one llm step, revised two.
        assert side["low"] == pytest.approx(_llm(2.5, 10, 0), abs=1e-6)
        assert side["high"] == pytest.approx(2 * _llm(2.5, 10, 1), abs=1e-6)
        assert body["models"]["unpriced"] == []
        assert (body["basis"], body["runs"], body["open"]) == ("steps", None, [])

    async def test_a_local_model_costs_nothing(self, client):
        body = (await client.post("/api/orchestrations/estimate",
                                  json={"definition": _definition("ollama.qwen3:8b")})).json()
        assert body["models"]["sides"] == {"local": {"low": 0.0, "high": 0.0, "open": []}}

    async def test_a_model_the_rate_card_does_not_know_is_named(self, client):
        body = (await client.post("/api/orchestrations/estimate",
                                  json={"definition": _definition("gpt-9-turbo")})).json()
        assert body["models"]["unpriced"] == ["gpt-9-turbo"]
        assert body["models"]["sides"] == {}

    async def test_a_bare_model_name_is_ollamas_and_free(self, client):
        # The engine sends a name with no provider prefix to Ollama (`provider_for`'s fallback).
        body = (await client.post("/api/orchestrations/estimate",
                                  json={"definition": _definition("llama3.2")})).json()
        assert set(body["models"]["sides"]) == {"local"}
        assert body["models"]["unpriced"] == []

    @pytest.mark.parametrize("payload", [{}, {"definition": "nope"}, {"definition": []}])
    async def test_a_body_without_a_definition_is_refused(self, client, payload):
        assert (await client.post("/api/orchestrations/estimate", json=payload)).status_code == 422

    async def test_finished_runs_since_the_last_edit_replace_the_prior(self, client, seed_orchestration):
        import core.orchestration.state as state_mod
        from core.models_orchestration import OrchestrationRun

        orch = await seed_orchestration(updated_at="2026-10-12T08:00:00Z", **_definition("gpt-4o"))
        for run_id, status, cost, started in [
            ("a", "completed", 0.05, "2026-10-12T09:00:00Z"),
            ("b", "completed", 0.02, "2026-10-12T09:30:00Z"),
            ("c", "failed", 0.90, "2026-10-12T09:45:00Z"),      # not finished well: not counted
            ("d", "completed", 0.70, "2026-10-12T07:00:00Z"),   # before the edit: not counted
        ]:
            await state_mod.SharedState(OrchestrationRun(
                run_id=f"run_{orch['id']}_{run_id}", orchestration_id=orch["id"], status=status,
                total_cost_usd=cost, started_at=started)).checkpoint()

        body = (await client.post("/api/orchestrations/estimate",
                                  json={"definition": _definition("gpt-4o"), "orchestration_id": orch["id"]})).json()
        assert body["basis"] == "runs"
        assert body["runs"] == {"count": 2, "low": 0.02, "high": 0.05, "open": []}


class TestEachStepSaysWhatItSpent:
    async def test_the_run_detail_sweeps_calls_into_steps_and_entries(self, client):
        import core.orchestration.state as state_mod
        from core.models_orchestration import OrchestrationRun
        from core.store import usage as usage_store

        t0 = datetime(2026, 10, 12, 7, 50, 3, tzinfo=UTC)

        def ms(seconds):
            return int((t0 + timedelta(seconds=seconds)).timestamp() * 1000)

        def entry(step_id, a, b):
            return {"step_id": step_id, "step_type": "llm", "status": "completed",
                    "started_at": "2026-10-12T07:50:03Z", "ended_at": "2026-10-12T07:50:10Z",
                    "started_ms": ms(a), "ended_ms": ms(b)}

        history = [entry("structure", 0, 2.69), entry("line_edit", 2.70, 4.11), entry("structure", 4.12, 6.0)]
        await state_mod.SharedState(OrchestrationRun(
            run_id="run_sweep_1", orchestration_id="orch_x", status="completed", step_history=history)).checkpoint()
        for seconds, cost in [(2.65, 0.01), (4.06, 0.02), (5.9, 0.03)]:
            await usage_store.append({
                "timestamp": t0 + timedelta(seconds=seconds), "model": "gpt-4o", "provider": "openai",
                "session_id": None, "agent_id": None, "source": "orchestration", "run_id": "run_sweep_1",
                "tool_name": None, "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                "context_chars": 0, "estimated_cost": cost, "latency_seconds": 0.5,
                "cache_read_tokens": 0, "cache_write_tokens": 0, "estimated_savings": 0.0,
                "response_cache_hit": False,
            })

        body = (await client.get("/api/orchestrations/runs/run_sweep_1")).json()
        assert body["step_usage"]["structure"]["turns"] == 2
        assert body["step_usage"]["structure"]["cost_usd"] == pytest.approx(0.04)
        assert body["step_usage"]["line_edit"]["cost_usd"] == pytest.approx(0.02)
        assert [e["cost_usd"] for e in body["entry_usage"]] == pytest.approx([0.01, 0.02, 0.03])

    async def test_a_run_with_no_calls_has_an_empty_breakdown(self, client):
        import core.orchestration.state as state_mod
        from core.models_orchestration import OrchestrationRun

        await state_mod.SharedState(OrchestrationRun(
            run_id="run_sweep_2", orchestration_id="orch_x", status="completed")).checkpoint()
        body = (await client.get("/api/orchestrations/runs/run_sweep_2")).json()
        assert (body["step_usage"], body["entry_usage"]) == ({}, [])
