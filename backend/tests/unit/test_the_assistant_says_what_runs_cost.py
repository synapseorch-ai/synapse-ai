"""
Synapse AI says what the workspace's runs cost: what was paid, credits where the host charges them, model dollars
always (F843, found by QA1a on prod; SAI2-P2, D308).

Its readers summed the engine's `total_cost_usd`, the sum of its calls' rate-card estimates. On cloud a managed call
(`synapse.*`) is priced by the gateway into the model ledger, never the rate card, so the engine's figure is 0: a
keyless workspace asking "what did that run cost us?" was told $0.00 for a run its run page shows at 5.3 credits and
$0.0024 (`kestrel`, 10-22). Every read of a run's cost now goes through one host hook (`product_tools.run_cost`):
self-hosted has no credits and its dollars are the engine's own estimate, as before; a host installs what its run
page says (cloud: `model_cost_usd`, the estimate plus what the wallet settled, and the meter's credits). `today`,
`usage` and `run` say both where both are known.
"""
from __future__ import annotations

import pytest

from core.product_tools import run_cost

from test_the_assistant_reads_what_happened import _ago, _read, _run, _workflow, viewer  # noqa: F401


@pytest.fixture(autouse=True)
def no_host():
    run_cost.set_run_costs(None)
    yield
    run_cost.set_run_costs(None)


@pytest.fixture
async def runs():
    await _workflow(id="orch_tri", name="Inbox triage")
    await _run("run_a", "orch_tri", "completed", started=_ago(hours=2), cost=0.0)     # managed: the engine says 0
    await _run("run_b", "orch_tri", "completed", started=_ago(hours=1), cost=0.004)   # an own-key call's estimate


def _managed(ids):
    """What cloud's run page says of these runs (QA1a's run, and one beside it)."""
    paid = {"run_a": {"model_dollars": 0.00242, "credits": 5.3}, "run_b": {"model_dollars": 0.004, "credits": 2.0}}

    async def costs(run_ids):
        ids.extend(run_ids)
        return {r: paid[r] for r in run_ids if r in paid}

    return costs


async def test_self_hosted_says_its_model_dollars_and_no_credits(runs, viewer):
    today = await _read(viewer, topic="today")
    assert today["model_dollars"] == pytest.approx(0.004) and "credits" not in today
    usage = await _read(viewer, topic="usage")
    [row] = usage["items"]
    assert row["model_dollars"] == pytest.approx(0.004) and "credits" not in row
    run = await _read(viewer, topic="run", run_id="run_b")
    assert run["model_dollars"] == pytest.approx(0.004) and "credits" not in run


async def test_a_managed_workspace_is_told_what_it_paid_in_credits_and_dollars(runs, viewer):
    asked: list = []
    run_cost.set_run_costs(_managed(asked))
    today = await _read(viewer, topic="today")
    assert today["credits"] == pytest.approx(7.3) and today["model_dollars"] == pytest.approx(0.00642)
    [row] = (await _read(viewer, topic="usage"))["items"]
    assert (row["runs"], row["credits"], row["model_dollars"]) == (2, pytest.approx(7.3), pytest.approx(0.00642))
    run = await _read(viewer, topic="run", run_id="run_a")
    assert run["credits"] == pytest.approx(5.3) and run["model_dollars"] == pytest.approx(0.00242)
    assert set(asked) == {"run_a", "run_b"}


async def test_a_host_that_cannot_say_falls_back_to_the_engine_s_estimate(runs, viewer):
    async def broken(run_ids):
        raise RuntimeError("ledger down")

    run_cost.set_run_costs(broken)
    today = await _read(viewer, topic="today")
    assert today["model_dollars"] == pytest.approx(0.004) and "credits" not in today


async def test_the_mcp_door_says_the_same(runs):
    """`summarise_activity` is a product tool: MCP's door reads the same hook."""
    from core.product_tools import areas, registry
    from core.product_tools.caller import Caller
    from core.product_tools.roles import Role

    areas.ensure_loaded()
    run_cost.set_run_costs(_managed([]))
    got = await registry.invoke("summarise_activity", Caller(role=Role.VIEWER, via="mcp"), {"days": 1})
    assert got["credits"] == pytest.approx(7.3) and got["model_dollars"] == pytest.approx(0.00642)
